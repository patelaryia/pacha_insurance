"""Production grader gating rule and the Phase-2 AR-2 callable seam."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from claim_core import new_ulid


@dataclass(frozen=True)
class GatingDecision:
    """A grade plus whether the future side-effect gate must block it."""

    grade: Any
    blocked: bool


def apply_gating(harness: Any, grade: Any, subject_ref: dict[str, Any], actor: str) -> bool:
    """Apply §3.3 without executing any side effect itself."""

    if grade.result != "fail":
        return False
    capability_id = subject_ref.get("capability_id")
    if grade.severity == "critical":
        claim_id = subject_ref.get("claim_id")
        with harness.sessions.begin() as session:
            harness.record_event(
                session,
                claim_id=claim_id if isinstance(claim_id, str) else None,
                event_type="review.created",
                payload={
                    "review_id": new_ulid(),
                    "type": "EXCEPTION",
                    "subtype": "grader_critical_fail",
                    "grader_id": grade.grader_id,
                    "subject_ref": subject_ref,
                },
                actor=actor,
                correlation_id=None,
            )
        return True
    if grade.severity == "major" and isinstance(capability_id, str):
        return harness.autonomy.level(capability_id) in {"L3", "L4"}
    return False


def grade_output(
    harness: Any,
    grader_id: str,
    subject_ref: dict[str, Any],
    *,
    actor: str,
) -> GatingDecision:
    """Grade one output and return the decision consumed by the future AR-2 gate."""

    grade = harness.grade(grader_id, subject_ref, actor)
    blocked = grade.result == "fail" and (
        grade.severity == "critical"
        or (
            grade.severity == "major"
            and isinstance(subject_ref.get("capability_id"), str)
            and harness.autonomy.level(subject_ref["capability_id"]) in {"L3", "L4"}
        )
    )
    return GatingDecision(grade=grade, blocked=blocked)


__all__ = ["GatingDecision", "apply_gating", "grade_output"]
