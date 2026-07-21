"""Estimate-verified trigger and dual-path mode decision for PRD-07 §7.3."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from agent_runtime import Action
from claim_core import STATE_METADATA, ClaimState, new_ulid

ACTOR = "agent:assessment"
COMMITTED_STATES = frozenset({"extracted", "human_verified", "system_confirmed"})
PROGRESSED_STATES = frozenset(
    {
        "IN_ASSESSMENT",
        "REPORT_RECEIVED",
        "REGISTERED",
        "RESERVED",
        "PACK_READY",
        "IN_APPROVAL",
        "APPROVED",
        "IN_REPAIR",
        "REINSPECTION",
        "RELEASED",
        "WRITE_OFF",
        "CLIENT_ELECTION",
        "SURRENDER_CHECKLIST",
        "SALVAGE_BIDDING",
        "RETAINED",
        "SETTLEMENT",
        "SETTLED",
        "CLOSED",
    }
)
SHADOW_SCHEMA = {
    "type": "object",
    "required": ["mode", "rationale", "confidence"],
    "properties": {
        "mode": {"enum": ["desk", "physical"]},
        "rationale": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "additionalProperties": False,
}


class AssessmentTrigger:
    """Advance eligible claims and issue one governed mode card per estimate window."""

    def __init__(self, app: Any, model_client: Any) -> None:
        self.app = app
        self.model_client = model_client
        self.sessions = sessionmaker(bind=app.state.engine, expire_on_commit=False)

    def _emit(
        self,
        *,
        claim_id: str,
        event_type: str,
        payload: dict[str, Any],
        correlation_id: str,
    ) -> str:
        with self.sessions.begin() as session:
            event = self.app.state.record_event(
                session,
                claim_id=claim_id,
                event_type=event_type,
                payload=payload,
                actor=ACTOR,
                correlation_id=correlation_id,
            )
            return event.id

    def _open_card(self, claim_id: str) -> bool:
        with self.app.state.engine.connect() as connection:
            value = connection.execute(
                text(
                    "SELECT id FROM review_items WHERE claim_id = :claim_id "
                    "AND type = 'MODE_CONFIRM' AND status = 'open' "
                    "ORDER BY created_at, id LIMIT 1"
                ),
                {"claim_id": claim_id},
            ).scalar()
        return isinstance(value, str)

    def _shadow(
        self,
        *,
        claim_id: str,
        mode_card_id: str,
        estimate_total: int,
        estimate_document_id: str | None,
        photos: list[str],
    ) -> None:
        _claim, fields, _blocked = self.app.state.claim_service.hydrate_claim(
            claim_id, ACTOR, paths=["loss.narrative"]
        )
        narrative = fields.get("loss.narrative")
        inputs = {
            "task": "assessment_mode_shadow",
            "estimate_total": estimate_total,
            "line_items_document_id": estimate_document_id,
            "damage_photo_document_ids": list(photos),
            "loss_narrative": narrative.value if narrative is not None else None,
            "vehicle_age": None,
        }
        try:
            result = self.model_client.structured_call(
                tier="MODEL_HEAVY", schema=SHADOW_SCHEMA, inputs=inputs
            )
            data = result.get("data") if isinstance(result, dict) else None
            if (
                not isinstance(data, dict)
                or data.get("mode") not in {"desk", "physical"}
                or not isinstance(data.get("confidence"), int | float)
                or isinstance(data.get("confidence"), bool)
            ):
                raise ValueError("assessment shadow returned an invalid structured result")
            log_payload = {
                "status": "completed",
                "mode_card_id": mode_card_id,
                "mode": data["mode"],
                "confidence": data["confidence"],
                "rationale": "__redacted__",
            }
        except Exception as error:  # Path B must not damage the governed Path A card.
            log_payload = {
                "status": "failed",
                "mode_card_id": mode_card_id,
                "error_type": type(error).__name__,
                "rationale": "__redacted__",
            }
        self.app.state.agent_runtime.execute_or_stage(
            capability_id="assessment.mode_shadow",
            action=Action(
                type="assessment.mode_shadow",
                payload={
                    "mode_card_id": mode_card_id,
                    "estimate_document_id": estimate_document_id,
                    "photo_count": len(photos),
                },
                log_payload=log_payload,
            ),
            claim_id=claim_id,
            actor=ACTOR,
        )

    def issue_from_payload(
        self,
        *,
        claim_id: str,
        action_payload: dict[str, Any],
        retry_of: str | None = None,
    ) -> str | None:
        if self._open_card(claim_id):
            return None
        payload = dict(action_payload)
        if retry_of is not None:
            payload["retry_of"] = retry_of
        outcome = self.app.state.agent_runtime.execute_or_stage(
            capability_id="assessment.mode_confirm",
            action=Action(type="assessment.mode_confirm", payload=payload),
            claim_id=claim_id,
            actor=ACTOR,
        )
        review_id = outcome.get("review_id")
        if outcome.get("status") != "staged" or not isinstance(review_id, str):
            return None
        self._emit(
            claim_id=claim_id,
            event_type="assessment.mode_item_created",
            payload={
                "claim_id": claim_id,
                "review_id": review_id,
                "retry_of": retry_of,
            },
            correlation_id=review_id,
        )
        try:
            self._shadow(
                claim_id=claim_id,
                mode_card_id=review_id,
                estimate_total=int(action_payload["estimate_total"]),
                estimate_document_id=action_payload.get("estimate_document_id"),
                photos=list(action_payload.get("photos") or []),
            )
        except Exception:
            return review_id
        return review_id

    def _out_of_sequence(self, claim_id: str, source_event_id: str, status: str) -> None:
        self._emit(
            claim_id=claim_id,
            event_type="review.created",
            payload={
                "review_id": new_ulid(),
                "type": "EXCEPTION",
                "subtype": "assessment_out_of_sequence",
                "source_event_id": source_event_id,
                "facts": {"claim_status": status, "field_path": "assessment.estimate_total"},
                "risk": "assessment dispatch would bypass the canonical claim lifecycle",
                "recommendation": "repair the claim state before reprocessing the estimate",
                "resolution_schema": "EXCEPTION@1",
            },
            correlation_id=source_event_id,
        )

    def _process_estimate(
        self,
        *,
        claim_id: str,
        source_event_id: str,
        expected_field_id: str | None = None,
    ) -> None:
        claim, fields, _blocked = self.app.state.claim_service.hydrate_claim(
            claim_id, ACTOR, paths=["assessment.estimate_total"]
        )
        estimate = fields.get("assessment.estimate_total")
        if (
            estimate is None
            or (expected_field_id is not None and estimate.id != expected_field_id)
            or estimate.verification_state not in COMMITTED_STATES
            or not isinstance(estimate.value, int)
            or isinstance(estimate.value, bool)
        ):
            return
        metadata = STATE_METADATA[ClaimState(claim.status)]
        if metadata.suppresses_activity:
            return
        if claim.status == "INTIMATED":
            return
        if claim.status == "TRIAGED":
            self.app.state.claim_service.transition_claim(
                claim.id, "AWAITING_DOCS", {"trigger": "estimate_verified"}, ACTOR
            )
            self.app.state.claim_service.transition_claim(
                claim.id, "IN_ASSESSMENT", {"trigger": "estimate_received"}, ACTOR
            )
        elif claim.status == "AWAITING_DOCS":
            self.app.state.claim_service.transition_claim(
                claim.id, "IN_ASSESSMENT", {"trigger": "estimate_received"}, ACTOR
            )
        elif claim.status not in PROGRESSED_STATES:
            self._out_of_sequence(claim.id, source_event_id, claim.status)
            return
        if self._open_card(claim.id):
            return
        result = self.app.state.cop_runtime.evaluate("R-06", claim.id, ACTOR)
        source_ref = estimate.source_ref if isinstance(estimate.source_ref, dict) else {}
        estimate_document_id = source_ref.get("document_id")
        photos = sorted(
            document.id
            for document in self.app.state.claim_service.documents(claim.id)
            if document.doc_type == "photo_damage"
        )
        self.issue_from_payload(
            claim_id=claim.id,
            action_payload={
                "estimate_total": estimate.value,
                "rule": {
                    "rule_id": "R-06",
                    "status": result.status,
                    "rule_run_id": result.rule_run_id,
                    "verdict": "undetermined",
                },
                "photos": photos,
                "estimate_document_id": (
                    estimate_document_id if isinstance(estimate_document_id, str) else None
                ),
            },
        )

    def consume(self, event: Any) -> None:
        if not isinstance(event.claim_id, str) or not isinstance(event.payload, dict):
            return
        if (
            event.type == "field.updated"
            and event.payload.get("path") == "assessment.estimate_total"
        ):
            field_id = event.payload.get("field_id")
            if isinstance(field_id, str):
                self._process_estimate(
                    claim_id=event.claim_id,
                    source_event_id=event.id,
                    expected_field_id=field_id,
                )
            return
        if (
            event.type == "claim.status_changed"
            and event.payload.get("to") == "TRIAGED"
        ):
            self._process_estimate(
                claim_id=event.claim_id,
                source_event_id=event.id,
            )


__all__ = ["ACTOR", "AssessmentTrigger"]
