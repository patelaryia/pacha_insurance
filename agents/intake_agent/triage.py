"""PRD-05 §5.4 Mode A coverage card and deterministic triage."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from claim_core import FieldWrite, new_ulid

ACTOR = "agent:intake"
KEYED_PATHS = [
    "policy.sum_insured",
    "policy.period_start",
    "policy.period_end",
    "policy.endorsement_ref",
    "policy.premium_paid",
    "policy.excess_protector",
]


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


class ModeATriage:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.sessions = sessionmaker(bind=app.state.engine, expire_on_commit=False)

    def _emit_review(
        self,
        context: Any,
        *,
        item_type: str,
        subtype: str,
        payload: dict[str, Any],
    ) -> str:
        review_id = new_ulid()
        with self.sessions.begin() as session:
            self.app.state.record_event(
                session,
                claim_id=context.claim_id,
                event_type="review.created",
                payload={
                    "review_id": review_id,
                    "type": item_type,
                    "subtype": subtype,
                    "agent_run_id": context.run_id,
                    "capability_id": "triage.coverage_check",
                    **payload,
                },
                actor=ACTOR,
                correlation_id=context.run_id,
            )
        return review_id

    def _coverage_review(self, context: Any) -> dict[str, Any] | None:
        with self.app.state.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT r.id, r.status, r.resolution, r.payload FROM review_items r "
                    "JOIN events e ON e.id = r.source_event_id "
                    "WHERE r.claim_id = :claim_id AND r.type = 'FIELD_VERIFY' "
                    "AND r.subtype = 'coverage_manual' ORDER BY e.seq DESC LIMIT 1"
                ),
                {"claim_id": context.claim_id},
            ).mappings().first()
        if row is None:
            return None
        values = dict(row)
        values["payload"] = _json_value(values["payload"])
        return values

    def _coverage_payload(self, claim_id: str) -> dict[str, Any]:
        _claim, fields, _blocked = self.app.state.claim_service.hydrate_claim(
            claim_id, ACTOR, paths=["policy.number"]
        )
        policy = fields.get("policy.number")
        return {
            "policy_number": None if policy is None else policy.value,
            "keyed_paths": list(KEYED_PATHS),
            "fraud_signals": self._fraud_signals(claim_id),
        }

    def _coverage_retry(self, context: Any, rejected_id: str) -> str:
        for event in self.app.state.claim_service.timeline(context.claim_id):
            if (
                event.type == "review.created"
                and event.payload.get("type") == "FIELD_VERIFY"
                and event.payload.get("subtype") == "coverage_manual"
                and event.payload.get("retry_of") == rejected_id
            ):
                review_id = event.payload.get("review_id")
                if isinstance(review_id, str):
                    return review_id
        return self._emit_review(
            context,
            item_type="FIELD_VERIFY",
            subtype="coverage_manual",
            payload={**self._coverage_payload(context.claim_id), "retry_of": rejected_id},
        )

    def _fraud_signals(self, claim_id: str) -> list[dict[str, Any]]:
        return [
            dict(event.payload)
            for event in self.app.state.claim_service.timeline(claim_id)
            if event.type == "fraud.signal"
        ]

    def _exception(self, context: Any, subtype: str, payload: dict[str, Any]) -> dict[str, Any]:
        review_id = self._emit_review(
            context,
            item_type="EXCEPTION",
            subtype=subtype,
            payload=payload,
        )
        return {"status": "awaiting_review", "review_id": review_id}

    def _ensure_excess(self, claim_id: str, fields: dict[str, Any]) -> int | None:
        existing = fields.get("policy.excess_amount")
        if existing is not None and isinstance(existing.value, int):
            return existing.value
        result = self.app.state.cop_runtime.execute_calc("C-01", claim_id, ACTOR)
        if result.status != "executed" or not isinstance(result.output, int):
            return None
        self.app.state.claim_service.write_fields(
            claim_id,
            [
                FieldWrite(
                    path="policy.excess_amount",
                    value=result.output,
                    value_type="money",
                    source_type="calc",
                    source_ref={
                        "calc_id": result.calc_id,
                        "calc_version": result.calc_version,
                    },
                    verification_state="system_confirmed",
                )
            ],
            ACTOR,
        )
        return result.output

    def _has_review(self, claim_id: str, item_type: str, subtype: str | None) -> bool:
        with self.app.state.engine.connect() as connection:
            clauses = "type = :type"
            params: dict[str, Any] = {"claim_id": claim_id, "type": item_type}
            if subtype is not None:
                clauses += " AND subtype = :subtype"
                params["subtype"] = subtype
            return connection.execute(
                text(
                    "SELECT 1 FROM review_items WHERE claim_id = :claim_id AND "
                    + clauses
                    + " LIMIT 1"
                ),
                params,
            ).scalar() is not None

    def step(self, context: Any) -> dict[str, Any]:
        if context.claim_id is None:
            raise ValueError("triage requires a claim")
        card = self._coverage_review(context)
        if card is None:
            review_id = self._emit_review(
                context,
                item_type="FIELD_VERIFY",
                subtype="coverage_manual",
                payload=self._coverage_payload(context.claim_id),
            )
            return {
                "status": "awaiting_review",
                "review_id": review_id,
                "resume_step": True,
            }
        if card["status"] == "open":
            return {
                "status": "awaiting_review",
                "review_id": card["id"],
                "resume_step": True,
            }
        if card["resolution"] not in {"approved", "edited"}:
            review_id = self._coverage_retry(context, card["id"])
            return {
                "status": "awaiting_review",
                "review_id": review_id,
                "resume_step": True,
            }

        paths = [
            *KEYED_PATHS,
            "policy.excess_amount",
            "loss.date",
            "assessment.estimate_total",
        ]
        claim, fields, _blocked = self.app.state.claim_service.hydrate_claim(
            context.claim_id, ACTOR, paths=paths
        )
        required = [
            "policy.sum_insured",
            "policy.period_start",
            "policy.period_end",
            "policy.premium_paid",
            "loss.date",
        ]
        missing = [path for path in required if path not in fields]
        if missing:
            return self._exception(
                context,
                "coverage_inputs_missing",
                {"missing_inputs": missing},
            )
        excess = self._ensure_excess(context.claim_id, fields)
        if excess is None:
            return self._exception(
                context,
                "coverage_inputs_missing",
                {"missing_inputs": ["policy.excess_amount"]},
            )
        loss_date = date.fromisoformat(str(fields["loss.date"].value))
        period_start = date.fromisoformat(str(fields["policy.period_start"].value))
        period_end = date.fromisoformat(str(fields["policy.period_end"].value))
        if not period_start <= loss_date <= period_end:
            return self._exception(
                context,
                "out_of_cover",
                {
                    "loss_date": loss_date.isoformat(),
                    "period_start": period_start.isoformat(),
                    "period_end": period_end.isoformat(),
                },
            )
        if fields["policy.premium_paid"].value is False:
            return self._exception(context, "premium_unpaid", {"premium_paid": False})

        r02 = self.app.state.cop_runtime.evaluate("R-02", context.claim_id, ACTOR)
        if r02.fired is True:
            if not self._has_review(context.claim_id, "DRAFT_RELEASE", "decline_draft"):
                self.app.state.cop_runtime.execute_outcome(r02, ACTOR)
            if not self._has_review(context.claim_id, "EX_GRATIA", None):
                r03 = self.app.state.cop_runtime.evaluate("R-03", context.claim_id, ACTOR)
                self.app.state.cop_runtime.execute_outcome(r03, ACTOR)
        if claim.status == "INTIMATED":
            self.app.state.claim_service.transition_claim(
                context.claim_id, "TRIAGED", {}, ACTOR
            )
        return {
            "status": "completed",
            "r02_status": r02.status,
            "r02_fired": r02.fired,
            "excess": excess,
        }


__all__ = ["KEYED_PATHS", "ModeATriage"]
