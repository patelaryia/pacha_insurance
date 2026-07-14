"""Governed autonomy levels, counters, promotions, sampling, and demotions."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import attributes

from claim_core import new_ulid
from eval_harness.models import AutonomyChange, Capability, GraderRun
from eval_harness.policies import LEVEL_RANK, CapabilityPolicy

LOGGER = logging.getLogger(__name__)
MATERIAL_KINDS = frozenset({"money", "date", "party", "enum"})


class PromotionDenied(RuntimeError):
    """A stable autonomy promotion denial returned by Python and HTTP surfaces."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ResolutionOutcome:
    """One counter observation derived from the interim v0 review payload."""

    event_id: str
    seq: int
    passed: bool
    material_edit: bool
    resolution: str


def is_material_edit(diff: Any) -> bool:
    """Apply the binding typed-field or >15% prose-change definition."""

    if not isinstance(diff, dict):
        return False
    changes = diff.get("typed_changes", [])
    if isinstance(changes, list) and any(
        isinstance(change, dict) and change.get("kind") in MATERIAL_KINDS
        for change in changes
    ):
        return True
    ratio = diff.get("prose_change_ratio", 0)
    return isinstance(ratio, (int, float)) and not isinstance(ratio, bool) and ratio > 0.15


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _truthy(value: Any) -> bool:
    value = _json_value(value)
    return value is True or (isinstance(value, str) and value.lower() == "true")


class AutonomyController:
    """The sole mutation surface for capability autonomy levels."""

    def __init__(self, harness: Any, policies: list[CapabilityPolicy]) -> None:
        self.harness = harness
        self.engine = harness.engine
        self.sessions = harness.sessions
        self.clock = harness.clock
        self._seed(policies)

    def _seed(self, policies: list[CapabilityPolicy]) -> None:
        with self.sessions.begin() as session:
            for policy in policies:
                row = session.get(Capability, policy.capability_id)
                if row is None:
                    session.add(
                        Capability(
                            id=policy.capability_id,
                            current_level=policy.initial_level,
                            max_level=policy.max_level,
                            policy=policy.policy,
                        )
                    )
                    continue
                # Existing databases may only tighten on a pack refresh.
                if LEVEL_RANK[policy.max_level] > LEVEL_RANK[row.max_level]:
                    raise ValueError(f"policy refresh widens ceiling for {policy.capability_id}")
                row.max_level = policy.max_level
                row.policy = policy.policy

    def _capability(self, capability_id: str) -> Capability:
        with self.sessions() as session:
            row = session.get(Capability, capability_id)
            if row is None:
                raise PromotionDenied("UNKNOWN_CAPABILITY")
            session.expunge(row)
            return row

    def level(self, capability_id: str) -> str:
        return self._capability(capability_id).current_level

    def _resolutions(
        self,
        capability_id: str,
        *,
        through_seq: int | None = None,
    ) -> list[ResolutionOutcome]:
        statement = "SELECT id, seq, payload FROM events WHERE type = 'review.resolved'"
        parameters: dict[str, Any] = {}
        if through_seq is not None:
            statement += " AND seq <= :through_seq"
            parameters["through_seq"] = through_seq
        statement += " ORDER BY seq"
        outcomes: list[ResolutionOutcome] = []
        with self.engine.connect() as connection:
            rows = connection.execute(text(statement), parameters)
            for event_id, seq, raw_payload in rows:
                payload = _json_value(raw_payload)
                if not isinstance(payload, dict) or payload.get("capability_id") != capability_id:
                    continue
                resolution = payload.get("resolution")
                material = is_material_edit(payload.get("diff"))
                passed = resolution == "approved" or (
                    resolution == "edited" and not material
                )
                outcomes.append(
                    ResolutionOutcome(
                        event_id=str(event_id),
                        seq=int(seq),
                        passed=passed,
                        material_edit=material,
                        resolution=str(resolution),
                    )
                )
        return outcomes

    def _grader_window(
        self,
        capability_id: str,
        size: int,
        *,
        since: datetime | None = None,
    ) -> list[GraderRun]:
        with self.sessions() as session:
            query = (
                select(GraderRun)
                .where(GraderRun.test_case_id.is_(None))
                .order_by(GraderRun.occurred_at.desc(), GraderRun.id.desc())
            )
            if since is not None:
                query = query.where(GraderRun.occurred_at >= since)
            rows = session.scalars(query)
            selected = [
                row
                for row in rows
                if (row.subject_ref or {}).get("capability_id") == capability_id
            ][:size]
            for row in selected:
                session.expunge(row)
            return selected

    @staticmethod
    def _pass_percent(outcomes: list[ResolutionOutcome]) -> float | int:
        if not outcomes:
            return 100
        return sum(outcome.passed for outcome in outcomes) * 100 / len(outcomes)

    @staticmethod
    def _grader_pass_percent(runs: list[GraderRun]) -> float | int:
        if not runs:
            return 100
        return sum(run.result == "pass" for run in runs) * 100 / len(runs)

    @staticmethod
    def _outcomes_meet(outcomes: list[ResolutionOutcome], threshold: int) -> bool:
        if not outcomes:
            return True
        return sum(outcome.passed for outcome in outcomes) * 100 >= threshold * len(
            outcomes
        )

    @staticmethod
    def _graders_meet(runs: list[GraderRun], threshold: int) -> bool:
        if not runs:
            return True
        return sum(run.result == "pass" for run in runs) * 100 >= threshold * len(runs)

    def evidence(
        self,
        capability_id: str,
        *,
        through_seq: int | None = None,
    ) -> dict[str, Any]:
        capability = self._capability(capability_id)
        resolutions = self._resolutions(capability_id, through_seq=through_seq)
        consecutive = 0
        for outcome in reversed(resolutions):
            if not outcome.passed:
                break
            consecutive += 1
        rolling_20 = resolutions[-20:]
        rolling_50 = resolutions[-50:]
        rolling_100 = resolutions[-100:]
        grader_size = {"L0": 25, "L1": 25, "L2": 50, "L3": 100, "L4": 100}[
            capability.current_level
        ]
        graders = self._grader_window(capability_id, grader_size)
        return {
            "current_level": capability.current_level,
            "max_level": capability.max_level,
            "resolution_count": len(resolutions),
            "consecutive_approvals": consecutive,
            "rolling_20_count": len(rolling_20),
            "rolling_20_pass_percent": self._pass_percent(rolling_20),
            "rolling_50_count": len(rolling_50),
            "rolling_50_pass_percent": self._pass_percent(rolling_50),
            "rolling_100_count": len(rolling_100),
            "rolling_100_pass_percent": self._pass_percent(rolling_100),
            "grader_window_count": len(graders),
            "grader_pass_percent": self._grader_pass_percent(graders),
            "critical_failures": sum(
                run.result == "fail" and run.severity == "critical" for run in graders
            ),
        }

    @staticmethod
    def should_sample(run_id: str, rate: int) -> bool:
        """Apply the exact reproducible SHA-256 selection vector."""

        if not isinstance(rate, int) or isinstance(rate, bool) or not 0 <= rate <= 100:
            raise ValueError("sampling rate must be an integer from 0 to 100")
        bucket = int(hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:8], 16) % 100
        return bucket < rate

    def emit_sample_review(
        self,
        capability_id: str,
        run_id: str,
        *,
        claim_id: str | None,
        underlying_type: str,
        actor: str,
    ) -> bool:
        """Emit the existing SAMPLE_REVIEW type when the configured selector chooses it."""

        capability = self._capability(capability_id)
        rate = int(capability.policy.get("sampling_rate", 0))
        if capability.current_level != "L3" or not self.should_sample(run_id, rate):
            return False
        with self.sessions.begin() as session:
            self.harness.record_event(
                session,
                claim_id=claim_id,
                event_type="review.created",
                payload={
                    "review_id": new_ulid(),
                    "type": "SAMPLE_REVIEW",
                    "capability_id": capability_id,
                    "run_id": run_id,
                    "underlying_type": underlying_type,
                    "already_executed": True,
                },
                actor=actor,
                correlation_id=run_id,
            )
        return True

    def _state(self, key: str) -> Any:
        with self.engine.connect() as connection:
            return connection.execute(
                text("SELECT value FROM platform_state WHERE key = :key"),
                {"key": key},
            ).scalar()

    def _criteria_met(self, capability: Capability, to_level: str) -> bool:
        evidence = self.evidence(capability.id)
        policy = capability.policy
        current = capability.current_level
        if LEVEL_RANK[to_level] != LEVEL_RANK[current] + 1:
            return False
        if current == "L0":
            # PRD-03 defines no L0→L1 evidence threshold. Keep shadow capabilities
            # fail-closed until the policy is captured rather than inventing one.
            return False
        if current == "L1":
            graders = self._grader_window(capability.id, int(policy["l1_l2_items"]))
            return (
                evidence["consecutive_approvals"] >= int(policy["l1_l2_items"])
                and self._graders_meet(
                    graders, int(policy["l1_l2_grader_pass_percent"])
                )
            )
        if current == "L2":
            size = int(policy["l2_l3_items"])
            graders = self._grader_window(capability.id, size)
            outcomes = self._resolutions(capability.id)[-size:]
            return (
                evidence["rolling_50_count"] >= size
                and self._outcomes_meet(outcomes, int(policy["l2_l3_pass_percent"]))
                and self._graders_meet(graders, int(policy["l2_l3_pass_percent"]))
                and not any(
                    run.result == "fail" and run.severity == "critical" for run in graders
                )
            )
        if current == "L3":
            size = int(policy["l3_l4_items"])
            since = self.clock() - timedelta(days=int(policy["l3_l4_zero_critical_days"]))
            recent = self._grader_window(capability.id, size, since=since)
            outcomes = self._resolutions(capability.id)[-size:]
            return (
                evidence["rolling_100_count"] >= size
                and self._outcomes_meet(outcomes, int(policy["l3_l4_pass_percent"]))
                and self._graders_meet(recent, int(policy["l3_l4_pass_percent"]))
                and not any(
                    run.result == "fail" and run.severity == "critical" for run in recent
                )
            )
        return False

    @staticmethod
    def _sign_offs_valid(from_level: str, sign_offs: list[dict[str, str]]) -> bool:
        actors_by_role: dict[str, set[str]] = {}
        for sign_off in sign_offs:
            actor = sign_off.get("actor")
            role = sign_off.get("role")
            if isinstance(actor, str) and isinstance(role, str):
                actors_by_role.setdefault(role, set()).add(actor)
        if from_level == "L3":
            cms = actors_by_role.get("claims_manager", set())
            mds = actors_by_role.get("md", set())
            return any(cm != md for cm in cms for md in mds)
        return bool(actors_by_role.get("claims_manager"))

    def request_promotion(
        self,
        capability_id: str,
        to_level: str,
        *,
        sign_offs: list[dict[str, str]],
        actor: str,
    ) -> dict[str, Any]:
        """Validate in the binding order and record one human-signed level bump."""

        capability = self._capability(capability_id)
        if _truthy(self._state("autonomy_promotions_frozen")):
            raise PromotionDenied("PROMOTIONS_FROZEN")
        if capability_id.startswith("settlement.") and not _truthy(self._state("gp1_open")):
            raise PromotionDenied("GATE_GP1_CLOSED")
        if to_level not in LEVEL_RANK or LEVEL_RANK[to_level] > LEVEL_RANK[capability.max_level]:
            raise PromotionDenied("CEILING_EXCEEDED")
        if not self._criteria_met(capability, to_level):
            raise PromotionDenied("CRITERIA_NOT_MET")
        if not self._sign_offs_valid(capability.current_level, sign_offs):
            raise PromotionDenied("SIGN_OFF_REQUIRED")

        evidence = self.evidence(capability_id)
        evidence["sign_offs"] = [dict(item) for item in sign_offs]
        now = self.clock()
        with self.sessions.begin() as session:
            query = select(Capability).where(Capability.id == capability_id)
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                query = query.with_for_update()
            row = session.scalar(query)
            if row is None or row.current_level != capability.current_level:
                raise PromotionDenied("CRITERIA_NOT_MET")
            from_level = row.current_level
            row.current_level = to_level
            if to_level == "L3":
                policy = dict(row.policy)
                policy["sampling_rate"] = max(
                    int(policy.get("sampling_floor", 5)),
                    int(policy.get("sampling_rate", 20)),
                )
                row.policy = policy
                attributes.flag_modified(row, "policy")
            change_id = new_ulid()
            session.add(
                AutonomyChange(
                    id=change_id,
                    capability_id=capability_id,
                    from_level=from_level,
                    to_level=to_level,
                    reason="promotion",
                    evidence=evidence,
                    approved_by=json.dumps(sign_offs, sort_keys=True, separators=(",", ":")),
                    occurred_at=now,
                )
            )
            self.harness.record_event(
                session,
                claim_id=None,
                event_type="autonomy.promoted",
                payload={
                    "change_id": change_id,
                    "capability_id": capability_id,
                    "from_level": from_level,
                    "to_level": to_level,
                },
                actor=actor,
                correlation_id=change_id,
            )
        # Keep the API's "change + ledger" contract synchronous while still using
        # the registered concurrency-one ledger queue consumer.
        self.harness.dispatcher.dispatch_once(consumers=["ledger"])
        return {
            "capability_id": capability_id,
            "from_level": capability.current_level,
            "to_level": to_level,
            "evidence": evidence,
        }

    def _trigger_processed(self, event_id: str) -> bool:
        with self.sessions() as session:
            rows = session.scalars(
                select(AutonomyChange).where(AutonomyChange.reason == "auto_demotion")
            )
            return any(row.evidence.get("trigger_event_id") == event_id for row in rows)

    def _demote(self, capability_id: str, event: Any, trigger: str) -> None:
        if self._trigger_processed(event.id):
            return
        with self.sessions.begin() as session:
            query = select(Capability).where(Capability.id == capability_id)
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                query = query.with_for_update()
            row = session.scalar(query)
            if row is None or row.current_level in {"L0", "L1"}:
                return
            from_level = row.current_level
            to_level = f"L{LEVEL_RANK[from_level] - 1}"
            evidence = self.evidence(capability_id, through_seq=event.seq)
            evidence.update({"trigger": trigger, "trigger_event_id": event.id})
            change_id = new_ulid()
            row.current_level = to_level
            session.add(
                AutonomyChange(
                    id=change_id,
                    capability_id=capability_id,
                    from_level=from_level,
                    to_level=to_level,
                    reason="auto_demotion",
                    evidence=evidence,
                    approved_by=None,
                    occurred_at=self.clock(),
                )
            )
            common = {
                "change_id": change_id,
                "capability_id": capability_id,
                "from_level": from_level,
                "to_level": to_level,
                "trigger": trigger,
            }
            self.harness.record_event(
                session,
                claim_id=None,
                event_type="autonomy.demoted",
                payload=common,
                actor="system",
                correlation_id=change_id,
            )
            self.harness.record_event(
                session,
                claim_id=None,
                event_type="ops.alert",
                payload={"subtype": "autonomy_auto_demotion", **common},
                actor="system",
                correlation_id=change_id,
            )

    def consume(self, event: Any) -> None:
        """Idempotently update counters/demotions from production events."""

        if event.type == "grader.failed":
            subject_ref = event.payload.get("subject_ref")
            if isinstance(subject_ref, dict) and subject_ref.get("test_case_id") is not None:
                return
            capability_id = event.payload.get("capability_id")
            if (
                isinstance(capability_id, str)
                and event.payload.get("severity") == "critical"
            ):
                try:
                    if self.level(capability_id) in {"L3", "L4"}:
                        self._demote(capability_id, event, "critical_grader_failure")
                except PromotionDenied:
                    LOGGER.info("ignored grader failure for unknown capability %s", capability_id)
            return
        if event.type != "review.resolved":
            return
        capability_id = event.payload.get("capability_id")
        if not isinstance(capability_id, str):
            return
        try:
            capability = self._capability(capability_id)
        except PromotionDenied:
            LOGGER.info("ignored resolution for unknown capability %s", capability_id)
            return
        if LEVEL_RANK[capability.current_level] < LEVEL_RANK["L2"]:
            return
        policy = capability.policy
        window_size = int(policy["demotion_items"])
        outcomes = self._resolutions(capability_id, through_seq=event.seq)[-window_size:]
        if (
            len(outcomes) == window_size
            and not self._outcomes_meet(
                outcomes, int(policy["demotion_pass_percent"])
            )
        ):
            self._demote(capability_id, event, "rolling_resolution_pass_rate")


__all__ = ["AutonomyController", "PromotionDenied", "is_material_edit"]
