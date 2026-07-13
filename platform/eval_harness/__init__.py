"""Public interface and application wiring for the PRD-03 eval harness."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from claim_core import Base, new_ulid
from eval_harness.api import build_router
from eval_harness.autonomy import AutonomyController, PromotionDenied
from eval_harness.gating import apply_gating
from eval_harness.graders import EvalConsumer, GraderRegistry, GraderResult
from eval_harness.models import AutonomyChange, Capability, GraderRun, TestCase
from eval_harness.policies import load_policies


class EvalHarness:
    """Application-scoped grader, gating, autonomy, and reporting service."""

    def __init__(self, app: Any, policy_path: Path) -> None:
        if not hasattr(app.state, "cop_runtime"):
            raise RuntimeError("build_eval_harness requires app.state.cop_runtime")
        self.app = app
        self.engine = app.state.engine
        self.clock = app.state.clock
        self.claim_service = app.state.claim_service
        self.blob_store = app.state.blob_store
        self.record_event = app.state.record_event
        self.dispatcher = app.state.dispatcher
        self.runtime = app.state.cop_runtime
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        Base.metadata.create_all(
            self.engine,
            tables=[
                TestCase.__table__,
                GraderRun.__table__,
                Capability.__table__,
                AutonomyChange.__table__,
            ],
        )
        self.autonomy = AutonomyController(self, load_policies(policy_path))
        self.graders = GraderRegistry(self)

    @staticmethod
    def _claim_id(subject_ref: dict[str, Any], engine: Any) -> str | None:
        claim_id = subject_ref.get("claim_id")
        if isinstance(claim_id, str):
            return claim_id
        lookup = None
        key = None
        if isinstance(subject_ref.get("calc_run_id"), str):
            lookup = "SELECT claim_id FROM calc_runs WHERE id = :run_id"
            key = subject_ref["calc_run_id"]
        elif isinstance(subject_ref.get("rule_run_id"), str):
            lookup = "SELECT claim_id FROM rule_runs WHERE id = :run_id"
            key = subject_ref["rule_run_id"]
        if lookup is None:
            return None
        with engine.connect() as connection:
            value = connection.execute(text(lookup), {"run_id": key}).scalar()
        return value if isinstance(value, str) else None

    def grade(
        self,
        grader_id: str,
        subject_ref: dict[str, Any],
        actor: str,
    ) -> GraderResult:
        """Run, persist, publish, and gate one grade without feedback execution."""

        grader = self.graders.get(grader_id)
        raw = grader.grade(subject_ref, actor)
        run_id = new_ulid()
        claim_id = self._claim_id(subject_ref, self.engine)
        with self.sessions.begin() as session:
            session.add(
                GraderRun(
                    id=run_id,
                    grader_id=grader.grader_id,
                    subject_type=grader.subject_type,
                    subject_ref=dict(subject_ref),
                    claim_id=claim_id,
                    test_case_id=subject_ref.get("test_case_id"),
                    result=raw.result,
                    severity=grader.severity,
                    detail=raw.detail,
                    occurred_at=self.clock(),
                )
            )
            event_payload = {
                "grader_run_id": run_id,
                "grader_id": grader.grader_id,
                "subject_ref": dict(subject_ref),
                "severity": grader.severity,
            }
            capability_id = subject_ref.get("capability_id")
            if isinstance(capability_id, str):
                event_payload["capability_id"] = capability_id
            self.record_event(
                session,
                claim_id=claim_id,
                event_type="grader.passed" if raw.result == "pass" else "grader.failed",
                payload=event_payload,
                actor=actor,
                correlation_id=run_id,
            )
        result = GraderResult(
            grader_id=grader.grader_id,
            subject_type=grader.subject_type,
            result=raw.result,
            severity=grader.severity,
            detail=raw.detail,
            grader_run_id=run_id,
        )
        apply_gating(self, result, subject_ref, actor)
        return result


def build_eval_harness(app: Any) -> EvalHarness:
    """Build, expose, route, and register the two synchronous consumers."""

    policy_path = (
        Path(__file__).resolve().parents[2]
        / "packs"
        / "motor"
        / "autonomy"
        / "policies.yaml"
    )
    harness = EvalHarness(app, policy_path)
    app.state.eval_harness = harness
    app.state.dispatcher.register_consumer("eval", EvalConsumer(harness))
    app.state.dispatcher.register_consumer("autonomy", harness.autonomy.consume)
    app.include_router(build_router(harness))
    return harness


__all__ = [
    "EvalHarness",
    "GraderResult",
    "PromotionDenied",
    "build_eval_harness",
]
