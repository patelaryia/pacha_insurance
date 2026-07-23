"""MODE_CONFIRM@2 resolution and governed per-assessor T-11 dispatch."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from assessment_agent.trigger import ACTOR, AssessmentTrigger
from assessment_agent.vendors import Vendor, VendorRegistry
from claim_core import ClaimCoreError, FieldWrite, field_dictionary

ATTACHMENT_TYPES = {
    "claim_form": "claim_form",
    "repair_estimate": "repair_estimate",
    "photo_damage": "photo_damage",
    "logbook": "logbook",
}


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


class AssessmentDispatch:
    """Validate officer picks, commit their decision, and stage N firm drafts."""

    def __init__(
        self,
        app: Any,
        vendors: VendorRegistry,
        trigger: AssessmentTrigger,
    ) -> None:
        self.app = app
        self.vendors = vendors
        self.trigger = trigger
        self.sessions = sessionmaker(bind=app.state.engine, expire_on_commit=False)

    def validate_resolution(
        self,
        item: Any,
        action: str,
        payload: dict[str, Any],
        _actor: str,
    ) -> None:
        if item.type != "MODE_CONFIRM":
            return
        decision = payload.get("decision")
        vendor_ids = decision.get("vendor_ids") if isinstance(decision, dict) else None
        if not isinstance(vendor_ids, list):
            return
        rows = self.vendors.active_assessors(vendor_ids)
        if [row.id for row in rows] != sorted(vendor_ids):
            raise ClaimCoreError(
                422,
                "VENDOR_NOT_REGISTERED",
                "Every selected vendor must be an active assessor",
            )
        if action in {"approve", "edit_approve"}:
            if not isinstance(item.claim_id, str):
                raise ClaimCoreError(
                    409,
                    "RESOLUTION_BLOCKED_ON_INPUTS",
                    "Assessment dispatch requires a claim-scoped mode decision",
                )
            self._broker_party(item.claim_id)

    def _emit(
        self,
        *,
        claim_id: str,
        event_type: str,
        payload: dict[str, Any],
        actor: str,
        correlation_id: str,
    ) -> str:
        with self.sessions.begin() as session:
            event = self.app.state.record_event(
                session,
                claim_id=claim_id,
                event_type=event_type,
                payload=payload,
                actor=actor,
                correlation_id=correlation_id,
            )
            return event.id

    def _review_payload(self, review_id: str) -> dict[str, Any]:
        with self.app.state.engine.connect() as connection:
            raw = connection.execute(
                text("SELECT payload FROM review_items WHERE id = :review_id"),
                {"review_id": review_id},
            ).scalar()
        return _json(raw)

    def _already_decided(self, claim_id: str, review_id: str) -> bool:
        return any(
            event.type == "assessment.mode_decided"
            and event.payload.get("review_id") == review_id
            for event in self.app.state.claim_service.timeline(claim_id)
        )

    def _assessor_party(self, claim_id: str, vendor: Vendor) -> str:
        parties = self.app.state.claim_service.create_parties(
            claim_id,
            [
                {
                    "role": "assessor",
                    "name": vendor.name,
                    "email": vendor.emails[0],
                    "meta": {"vendor_id": vendor.id},
                }
            ],
            actor=ACTOR,
        )
        return parties[0].id

    def _broker_party(self, claim_id: str) -> str:
        with self.app.state.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, meta FROM parties WHERE claim_id = :claim_id "
                    "AND role IN ('broker', 'agent') ORDER BY id"
                ),
                {"claim_id": claim_id},
            ).all()
        matching = [
            str(row[0])
            for row in rows
            if _json(row[1]).get("source") == "intimation_sender"
        ]
        if len(matching) != 1:
            raise ClaimCoreError(
                409,
                "RESOLUTION_BLOCKED_ON_INPUTS",
                "Assessment dispatch requires one captured broker recipient",
            )
        return matching[0]

    def _merge(self, claim_id: str, mode: str, actor: str) -> tuple[dict[str, Any], list[str]]:
        _claim, fields, _blocked = self.app.state.claim_service.hydrate_claim(
            claim_id,
            actor,
            paths=["parties.insured.name", "vehicle.reg", "loss.narrative"],
        )
        values = {
            "claim_ref": claim_id,
            "insured_name": (
                fields["parties.insured.name"].value
                if "parties.insured.name" in fields
                else None
            ),
            "vehicle_reg": fields["vehicle.reg"].value if "vehicle.reg" in fields else None,
            "loss_summary": (
                fields["loss.narrative"].value if "loss.narrative" in fields else None
            ),
            "mode": mode,
        }
        missing = [key for key, value in values.items() if value in {None, ""}]
        missing.append("garage.details")
        return values, missing

    def _attachments(self, claim_id: str) -> tuple[list[str], list[str]]:
        documents = self.app.state.claim_service.documents(claim_id)
        held = [
            document.id
            for document in documents
            if document.doc_type in set(ATTACHMENT_TYPES.values())
        ]
        present = {document.doc_type for document in documents}
        missing = [
            item_id
            for item_id, doc_type in ATTACHMENT_TYPES.items()
            if doc_type not in present
        ]
        return held, missing

    def _write_decision(
        self,
        *,
        claim_id: str,
        review_id: str,
        mode: str,
        vendor_ids: list[str],
        actor: str,
    ) -> None:
        dictionary = field_dictionary()
        writes = [
            FieldWrite(
                path="assessment.mode",
                value=mode,
                value_type=dictionary["assessment.mode"].value_type,
                source_type="human",
                source_ref={"user_id": actor, "review_item_id": review_id},
                verification_state="human_verified",
            )
        ]
        if len(vendor_ids) > 1:
            writes.append(
                FieldWrite(
                    path="assessment.multi_mode",
                    value=True,
                    value_type=dictionary["assessment.multi_mode"].value_type,
                    source_type="human",
                    source_ref={"user_id": actor, "review_item_id": review_id},
                    verification_state="human_verified",
                )
            )
        self.app.state.claim_service.write_fields(claim_id, writes, actor)

    def _dispatch_vendor(
        self,
        *,
        claim_id: str,
        review_id: str,
        mode: str,
        vendor: Vendor,
        broker_party_id: str,
        actor: str,
    ) -> None:
        if any(
            event.type == "assessment.dispatched"
            and event.payload.get("review_id") == review_id
            and event.payload.get("vendor_id") == vendor.id
            for event in self.app.state.claim_service.timeline(claim_id)
        ):
            return
        assessor_party_id = self._assessor_party(claim_id, vendor)
        merge, missing = self._merge(claim_id, mode, actor)
        attachments, missing_attachments = self._attachments(claim_id)
        outcome = self.app.state.agent_runtime.comms.send(
            template_id="T-11",
            claim_id=claim_id,
            to_party_ids=[assessor_party_id, broker_party_id],
            attachments=tuple(attachments),
            capability_id="assessment.dispatch",
            actor=ACTOR,
            action_payload={
                "vendor_id": vendor.id,
                "mode": mode,
                "merge": merge,
                "missing": missing,
                "missing_attachments": missing_attachments,
            },
        )
        if outcome["status"] not in {"staged", "executed"}:
            return
        dispatch_event_id = self._emit(
            claim_id=claim_id,
            event_type="assessment.dispatched",
            payload={
                "claim_id": claim_id,
                "review_id": review_id,
                "vendor_id": vendor.id,
                "assessor_party_id": assessor_party_id,
                "gate_status": outcome["status"],
            },
            actor=ACTOR,
            correlation_id=review_id,
        )
        self.app.state.chase_agent.checklist.create_assessor_report(
            claim_id=claim_id,
            requester_party_id=assessor_party_id,
            correlation_id=dispatch_event_id,
        )

    def consume(self, event: Any) -> None:
        if (
            event.type != "review.resolved"
            or not isinstance(event.claim_id, str)
            or not isinstance(event.payload, dict)
            or event.payload.get("type") != "MODE_CONFIRM"
        ):
            return
        review_id = event.payload.get("review_id")
        if not isinstance(review_id, str):
            return
        original = self._review_payload(review_id)
        action_payload = original.get("action", {}).get("payload", {})
        if event.payload.get("resolution") == "rejected":
            if isinstance(action_payload, dict):
                action_payload = dict(action_payload)
                action_payload.pop("retry_of", None)
                self.trigger.issue_from_payload(
                    claim_id=event.claim_id,
                    action_payload=action_payload,
                    retry_of=review_id,
                )
            return
        if event.payload.get("resolution") not in {"approved", "edited"}:
            return
        if self._already_decided(event.claim_id, review_id):
            return
        decision = event.payload.get("decision")
        if not isinstance(decision, dict):
            return
        mode = decision.get("mode")
        vendor_ids = decision.get("vendor_ids")
        if mode not in {"desk", "physical"} or not isinstance(vendor_ids, list):
            return
        vendors = self.vendors.active_assessors(vendor_ids)
        broker_party_id = self._broker_party(event.claim_id)
        self._write_decision(
            claim_id=event.claim_id,
            review_id=review_id,
            mode=mode,
            vendor_ids=vendor_ids,
            actor=event.actor,
        )
        self._emit(
            claim_id=event.claim_id,
            event_type="assessment.mode_decided",
            payload={
                "claim_id": event.claim_id,
                "review_id": review_id,
                "mode": mode,
                "vendor_ids": list(vendor_ids),
                "label": "training_data",
            },
            actor=event.actor,
            correlation_id=review_id,
        )
        for vendor in vendors:
            self._dispatch_vendor(
                claim_id=event.claim_id,
                review_id=review_id,
                mode=mode,
                vendor=vendor,
                broker_party_id=broker_party_id,
                actor=event.actor,
            )


__all__ = ["AssessmentDispatch"]
