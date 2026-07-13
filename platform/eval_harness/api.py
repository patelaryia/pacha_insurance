"""First-class PRD-03 reporting and promotion routes."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from statistics import median
from typing import Any

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from eval_harness.autonomy import PromotionDenied
from eval_harness.models import Capability, GraderRun, TestCase
from eval_harness.policies import LEVEL_RANK


class PromotionRequest(BaseModel):
    """Human sign-offs for one requested adjacent-level promotion."""

    to_level: str
    sign_offs: list[dict[str, str]] = Field(default_factory=list)


def build_router(harness: Any) -> APIRouter:
    """Create routes bound to one application-scoped harness."""

    router = APIRouter(prefix="/eval", tags=["eval"])

    @router.get("/capabilities")
    def capabilities(_x_actor: str = Header(alias="X-Actor")) -> dict[str, Any]:
        with harness.sessions() as session:
            rows = list(session.scalars(select(Capability).order_by(Capability.id)))
            response = []
            for row in rows:
                evidence = harness.autonomy.evidence(row.id)
                if LEVEL_RANK[row.current_level] >= LEVEL_RANK[row.max_level]:
                    runs_to_promotion = 0
                elif row.current_level == "L0":
                    runs_to_promotion = None
                elif row.current_level == "L1":
                    runs_to_promotion = max(
                        0,
                        int(row.policy["l1_l2_items"])
                        - evidence["consecutive_approvals"],
                    )
                elif row.current_level == "L2":
                    runs_to_promotion = max(
                        0,
                        int(row.policy["l2_l3_items"])
                        - evidence["rolling_50_count"],
                    )
                else:
                    runs_to_promotion = max(
                        0,
                        int(row.policy["l3_l4_items"])
                        - evidence["rolling_100_count"],
                    )
                response.append(
                    {
                        "id": row.id,
                        "current_level": row.current_level,
                        "max_level": row.max_level,
                        "pass_rate_window": evidence["grader_pass_percent"],
                        "consecutive_approvals": evidence["consecutive_approvals"],
                        "runs_to_promotion": runs_to_promotion,
                        "sampling_rate": int(row.policy.get("sampling_rate", 0)),
                    }
                )
        return {"capabilities": response}

    @router.post("/capabilities/{capability_id}/promote", response_model=None)
    def promote(
        capability_id: str,
        body: PromotionRequest,
        x_actor: str = Header(alias="X-Actor"),
    ) -> Any:
        try:
            return harness.autonomy.request_promotion(
                capability_id,
                body.to_level,
                sign_offs=body.sign_offs,
                actor=x_actor,
            )
        except PromotionDenied as error:
            return JSONResponse(status_code=403, content={"code": error.code})

    @router.get("/runs")
    def runs(
        capability: str | None = None,
        grader: str | None = None,
        _x_actor: str = Header(alias="X-Actor"),
    ) -> dict[str, Any]:
        with harness.sessions() as session:
            query = select(GraderRun).order_by(
                GraderRun.occurred_at.desc(), GraderRun.id.desc()
            )
            rows = list(session.scalars(query.limit(2000)))
        selected = []
        for row in rows:
            subject_ref = row.subject_ref or {}
            if capability is not None and subject_ref.get("capability_id") != capability:
                continue
            if grader is not None and row.grader_id != grader:
                continue
            selected.append(
                {
                    "id": row.id,
                    "grader_id": row.grader_id,
                    "subject_type": row.subject_type,
                    "subject_ref": subject_ref,
                    "claim_id": row.claim_id,
                    "test_case_id": row.test_case_id,
                    "result": row.result,
                    "severity": row.severity,
                    "detail": row.detail,
                    "occurred_at": row.occurred_at.isoformat(),
                }
            )
            if len(selected) == 200:
                break
        return {"runs": selected}

    @router.get("/corpus/stats")
    def corpus_stats(_x_actor: str = Header(alias="X-Actor")) -> dict[str, Any]:
        with harness.sessions() as session:
            cases = list(session.scalars(select(TestCase)))
        by_origin = Counter(case.origin for case in cases)
        by_tag: Counter[str] = Counter()
        for case in cases:
            by_tag.update(case.tags or [])
        return {
            "total": len(cases),
            "by_origin": dict(sorted(by_origin.items())),
            "by_tag": dict(sorted(by_tag.items())),
        }

    @router.get("/series")
    def series(
        _x_actor: str = Header(alias="X-Actor"),
        _window: int = Query(default=30, ge=1, le=365),
    ) -> dict[str, Any]:
        with harness.sessions() as session:
            capabilities_rows = list(session.scalars(select(Capability)))
            grader_rows = list(session.scalars(select(GraderRun)))
        total_capabilities = len(capabilities_rows)
        autonomous = sum(row.current_level in {"L3", "L4"} for row in capabilities_rows)
        autonomy_rate = (
            round(autonomous * 100 / total_capabilities, 2) if total_capabilities else 0
        )

        accuracy_counts: dict[str, list[bool]] = {}
        for run in grader_rows:
            capability_id = (run.subject_ref or {}).get("capability_id")
            if isinstance(capability_id, str):
                accuracy_counts.setdefault(capability_id, []).append(run.result == "pass")
        accuracy = {
            capability_id: round(sum(values) * 100 / len(values), 2)
            for capability_id, values in sorted(accuracy_counts.items())
        }

        created: dict[str, datetime] = {}
        resolutions = 0
        untouched = 0
        review_seconds: list[int] = []
        with harness.engine.connect() as connection:
            events = connection.exec_driver_sql(
                "SELECT type, payload, occurred_at FROM events "
                "WHERE type IN ('review.created','review.resolved') ORDER BY seq"
            )
            for event_type, raw_payload, occurred_at in events:
                payload = raw_payload
                if isinstance(payload, str):
                    payload = json.loads(payload)
                if not isinstance(payload, dict):
                    continue
                review_id = payload.get("review_id")
                if event_type == "review.created" and isinstance(review_id, str):
                    created[review_id] = occurred_at
                elif event_type == "review.resolved":
                    resolutions += 1
                    if payload.get("resolution") == "approved":
                        untouched += 1
                    if isinstance(review_id, str) and review_id in created:
                        delta = occurred_at - created[review_id]
                        review_seconds.append(max(0, int(delta.total_seconds())))
        no_touch_rate = round(untouched * 100 / resolutions, 2) if resolutions else 0
        median_review = int(median(review_seconds)) if review_seconds else 0
        return {
            "autonomy_rate": autonomy_rate,
            "no_touch_rate": no_touch_rate,
            "accuracy_by_capability": accuracy,
            "median_review_time_seconds": median_review,
        }

    return router


__all__ = ["build_router"]
