"""Exact, never-guess inbound document matching for PRD-06."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select, text

from chase_agent.checklist import ACTOR, ChecklistService, aware
from chase_agent.models import ChaseChecklist, ChaseItem


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


class ChaseMatcher:
    """Advance one exact checklist item from classification and extraction facts."""

    def __init__(self, app: Any, checklist: ChecklistService) -> None:
        self.app = app
        self.checklist = checklist
        schema_root = Path(__file__).resolve().parents[2] / "platform/doc_intel/schemas/motor"
        self.no_target_doc_types: set[str] = set()
        for definition in checklist.registry.values():
            doc_type = definition.get("doc_type")
            if not isinstance(doc_type, str) or doc_type in self.no_target_doc_types:
                continue
            schema_path = schema_root / f"{doc_type}.yaml"
            if not schema_path.exists():
                continue
            schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
            fields = schema.get("fields") if isinstance(schema, dict) else None
            if isinstance(fields, dict) and not any(
                isinstance(row, dict) and isinstance(row.get("target_path"), str)
                for row in fields.values()
            ):
                self.no_target_doc_types.add(doc_type)

    def _document(self, document_id: str) -> tuple[str, str] | None:
        with self.app.state.engine.connect() as connection:
            row = connection.execute(
                text("SELECT claim_id, doc_type FROM documents WHERE id = :id"),
                {"id": document_id},
            ).first()
        if row is None or not isinstance(row[1], str):
            return None
        return str(row[0]), str(row[1])

    def _candidates(
        self, session: Any, claim_id: str, doc_type: str, document_id: str
    ) -> list[tuple[ChaseChecklist, ChaseItem]]:
        linked = session.execute(
            select(ChaseChecklist, ChaseItem)
            .join(ChaseItem, ChaseItem.checklist_id == ChaseChecklist.id)
            .where(
                ChaseChecklist.claim_id == claim_id,
                ChaseChecklist.status == "open",
                ChaseItem.document_id == document_id,
                ChaseItem.state.in_(("received", "verified", "rejected")),
            )
            .order_by(ChaseItem.item_id, ChaseItem.id)
        ).all()
        if linked:
            return [(row[0], row[1]) for row in linked]
        matching_ids = sorted(
            item_id
            for item_id, definition in self.checklist.registry.items()
            if definition.get("kind") == "document"
            and definition.get("doc_type") == doc_type
        )
        if not matching_ids:
            return []
        rows = session.execute(
            select(ChaseChecklist, ChaseItem)
            .join(ChaseItem, ChaseItem.checklist_id == ChaseChecklist.id)
            .where(
                ChaseChecklist.claim_id == claim_id,
                ChaseChecklist.status == "open",
                ChaseItem.item_id.in_(matching_ids),
                ChaseItem.state.in_(("pending", "requested", "rejected")),
            )
            .order_by(ChaseItem.item_id, ChaseItem.id)
        ).all()
        return [(row[0], row[1]) for row in rows]

    def _cc1_mismatch(self, claim_id: str, document_id: str) -> bool:
        with self.app.state.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT payload FROM events WHERE claim_id = :claim_id "
                    "AND type = 'review.created' ORDER BY seq"
                ),
                {"claim_id": claim_id},
            ).scalars()
            for raw in rows:
                payload = _payload(raw)
                if (
                    payload.get("type") == "CONSISTENCY_FLAG"
                    and payload.get("subtype") == "CC-1"
                    and payload.get("document_id") == document_id
                    and payload.get("status") == "inconsistent"
                ):
                    return True
        return False

    def _rerequest_exists(
        self, claim_id: str, item_id: str, document_id: str
    ) -> bool:
        with self.app.state.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT payload FROM events WHERE claim_id = :claim_id "
                    "AND type = 'review.created' ORDER BY seq"
                ),
                {"claim_id": claim_id},
            ).scalars()
            for raw in rows:
                payload = _payload(raw)
                action = payload.get("action", {}).get("payload", {})
                if (
                    payload.get("capability_id") == "chase.rerequest"
                    and action.get("defect", {}).get("item_id") == item_id
                    and action.get("source_document_id") == document_id
                ):
                    return True
        return False

    def _stage_rerequest(
        self,
        *,
        checklist_id: str,
        claim_id: str,
        item_id: str,
        reason: str,
        document_id: str,
    ) -> None:
        if self._rerequest_exists(claim_id, item_id, document_id):
            return
        requester, tone = self.checklist.requester(claim_id)
        if requester is None:
            self.checklist._exception_once(
                claim_id=claim_id,
                subtype="chase_requester_missing",
                identity={"checklist_id": checklist_id},
                payload={"items": [item_id]},
            )
            return
        now = aware(self.app.state.clock())
        context = self.checklist.summary_payload(checklist_id, now=now)
        context["defect"] = {"item_id": item_id, "reason": reason}
        context["source_document_id"] = document_id
        self.app.state.agent_runtime.comms.send(
            template_id=f"T-06r-{tone}",
            claim_id=claim_id,
            to_party_ids=[requester],
            attachments=(),
            capability_id="chase.rerequest",
            actor=ACTOR,
            action_payload=context,
        )

    def consume(self, event: Any) -> None:
        if event.type not in {"document.classified", "document.extracted"}:
            return
        if not isinstance(event.payload, dict):
            return
        document_id = event.payload.get("document_id")
        if not isinstance(document_id, str):
            return
        document = self._document(document_id)
        if document is None:
            return
        claim_id, stored_doc_type = document
        doc_type = event.payload.get("doc_type", stored_doc_type)
        if not isinstance(doc_type, str):
            return
        rejected: tuple[str, str, str, str, str] | None = None
        with self.checklist.sessions.begin() as session:
            candidates = self._candidates(session, claim_id, doc_type, document_id)
            if len(candidates) != 1:
                if len(candidates) > 1:
                    self.checklist._exception_once(
                        claim_id=claim_id,
                        subtype="chase_match_ambiguous",
                        identity={"document_id": document_id},
                        payload={"candidate_item_ids": [item.item_id for _, item in candidates]},
                    )
                return
            checklist, item = candidates[0]
            if item.state == "verified":
                return
            occurred_at = aware(event.occurred_at)
            if item.state != "received":
                item.state = "received"
                item.received_at = occurred_at
                item.document_id = document_id
                item.reject_reason = None
                self.checklist._emit(
                    session,
                    claim_id=claim_id,
                    event_type="chase.item_received",
                    payload={
                        **self.checklist.event_payload(checklist, item),
                        "document_id": document_id,
                        "doc_type": doc_type,
                    },
                    correlation_id=event.id,
                )
            should_decide = (
                event.type == "document.extracted" or doc_type in self.no_target_doc_types
            )
            if not should_decide:
                return
            committed = event.payload.get("committed_paths")
            reason = None
            if self._cc1_mismatch(claim_id, document_id):
                reason = "wrong_vehicle"
            elif doc_type not in self.no_target_doc_types and not (
                isinstance(committed, list) and bool(committed)
            ):
                reason = "illegible"
            if reason is None:
                item.state = "verified"
                item.verified_at = occurred_at
                self.checklist._emit(
                    session,
                    claim_id=claim_id,
                    event_type="chase.item_verified",
                    payload={
                        **self.checklist.event_payload(checklist, item),
                        "document_id": document_id,
                        "doc_type": doc_type,
                    },
                    correlation_id=event.id,
                )
                self.checklist.maybe_complete(session, checklist)
            else:
                item.state = "rejected"
                item.reject_reason = reason
                self.checklist._emit(
                    session,
                    claim_id=claim_id,
                    event_type="chase.item_rejected",
                    payload={
                        **self.checklist.event_payload(checklist, item),
                        "document_id": document_id,
                        "reason": reason,
                    },
                    correlation_id=event.id,
                )
                rejected = (checklist.id, claim_id, item.item_id, reason, document_id)
        if rejected is not None:
            self._stage_rerequest(
                checklist_id=rejected[0],
                claim_id=rejected[1],
                item_id=rejected[2],
                reason=rejected[3],
                document_id=rejected[4],
            )


__all__ = ["ChaseMatcher"]
