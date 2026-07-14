"""Public interface and application wiring for the PRD-03 eval harness."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from claim_core import Base, new_ulid
from doc_intel.llm import ModelBudgetExceeded, ModelClient, ModelWrapper
from eval_harness.api import build_router
from eval_harness.autonomy import AutonomyController, PromotionDenied
from eval_harness.corpus import CorpusBatchResult, CorpusExecutor, CorpusObservation, CorpusService
from eval_harness.gating import apply_gating
from eval_harness.graders import EvalConsumer, GraderRegistry, GraderResult
from eval_harness.models import AutonomyChange, Capability, GraderRun, TestCase
from eval_harness.policies import load_policies
from eval_harness.tasks import configure_weekly_task


class EvalHarness:
    """Application-scoped grader, gating, autonomy, and reporting service."""

    def __init__(
        self,
        app: Any,
        policy_path: Path,
        harness_path: Path,
        *,
        model_client: ModelClient | None,
        corpus_executor: CorpusExecutor | None,
    ) -> None:
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
        self.model_client = model_client
        self.config = self._load_config(harness_path)
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
        self.corpus = CorpusService(self, corpus_executor, self.config["weekly"])

    @staticmethod
    def _load_config(path: Path) -> dict[str, Any]:
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as error:
            raise ValueError(f"invalid eval harness config: {error}") from error
        if not isinstance(payload, dict):
            raise ValueError("eval harness config must be a mapping")
        graders = payload.get("model_graders")
        weekly = payload.get("weekly")
        if not isinstance(graders, dict) or not isinstance(weekly, dict):
            raise ValueError("eval harness config requires model_graders and weekly")
        return payload

    def model_call(self, grader_id: str, *, schema: dict, inputs: dict) -> dict[str, Any]:
        """Call a configured grader only through the shared ED-4a wrapper."""

        if self.model_client is None:
            raise RuntimeError("model grader client is not configured")
        grader_config = self.config["model_graders"].get(grader_id)
        if not isinstance(grader_config, dict):
            raise RuntimeError(f"model grader config missing for {grader_id}")
        tier = grader_config.get("tier")
        task = grader_config.get("task")
        ceiling = grader_config.get("max_cost_usd")
        if (
            not isinstance(tier, str)
            or not isinstance(task, str)
            or not isinstance(ceiling, (int, float))
            or isinstance(ceiling, bool)
        ):
            raise RuntimeError(f"model grader config invalid for {grader_id}")
        configured_inputs = {**inputs, "task": task}
        result = ModelWrapper(self.model_client).structured_call(
            tier=tier,
            schema=schema,
            inputs=configured_inputs,
        )
        if result["cost_usd"] > ceiling:
            raise ModelBudgetExceeded(f"{grader_id} exceeded its configured call budget")
        return result

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
        if not isinstance(subject_ref.get("test_case_id"), str):
            apply_gating(self, result, subject_ref, actor)
        return result


def build_eval_harness(
    app: Any,
    *,
    model_client: ModelClient | None = None,
    corpus_executor: CorpusExecutor | None = None,
) -> EvalHarness:
    """Build, expose, route, and register the two synchronous consumers."""

    policy_path = (
        Path(__file__).resolve().parents[2]
        / "packs"
        / "motor"
        / "autonomy"
        / "policies.yaml"
    )
    harness_path = (
        Path(__file__).resolve().parents[2]
        / "packs"
        / "motor"
        / "eval"
        / "harness.yaml"
    )
    harness = EvalHarness(
        app,
        policy_path,
        harness_path,
        model_client=model_client,
        corpus_executor=corpus_executor,
    )
    app.state.eval_harness = harness
    app.state.dispatcher.register_consumer("eval", EvalConsumer(harness))
    app.state.dispatcher.register_consumer("autonomy", harness.autonomy.consume)
    app.state.dispatcher.register_consumer("correction_capture", harness.corpus.consume)
    configure_weekly_task(harness)
    app.include_router(build_router(harness))
    return harness


__all__ = [
    "EvalHarness",
    "CorpusBatchResult",
    "CorpusExecutor",
    "CorpusObservation",
    "GraderResult",
    "PromotionDenied",
    "build_eval_harness",
]
