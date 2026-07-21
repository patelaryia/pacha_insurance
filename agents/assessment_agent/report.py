"""Assessor-report attribution and verified-cascade arming."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from assessment_agent.cascade import AssessmentCascade, _json
from assessment_agent.selection import AssessmentSelection
from assessment_agent.trigger import ACTOR
from claim_core import new_ulid


class AssessmentReport:
    """Attribute reports by captured sender address and never by elimination."""

    def __init__(
        self,
        app: Any,
        cascade: AssessmentCascade,
        selection: AssessmentSelection,
    ) -> None:
        self.app = app
        self.cascade = cascade
        self.selection = selection

    def _document(self, document_id: str) -> dict[str, Any] | None:
        with self.app.state.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT claim_id, doc_type, source FROM documents WHERE id = :id"
                ),
                {"id": document_id},
            ).mappings().first()
        if row is None:
            return None
        return {**dict(row), "source": _json(row["source"])}

    def _sender(self, source: Any) -> str | None:
        if not isinstance(source, dict):
            return None
        source_ref = source.get("source_ref")
        if not isinstance(source_ref, str) or not source_ref:
            return None
        graph_message_id = source_ref.rsplit(":", 1)[0]
        with self.app.state.engine.connect() as connection:
            sender = connection.execute(
                text(
                    "SELECT from_addr FROM communications "
                    "WHERE graph_message_id = :graph_message_id LIMIT 1"
                ),
                {"graph_message_id": graph_message_id},
            ).scalar()
        return sender.strip().casefold() if isinstance(sender, str) and sender.strip() else None

    def _dispatched_parties(self, claim_id: str) -> set[str]:
        return {
            event["payload"]["assessor_party_id"]
            for event in self.cascade._events(claim_id, "assessment.dispatched")
            if isinstance(event["payload"].get("assessor_party_id"), str)
        }

    def _match_party(
        self, claim_id: str, sender: str | None
    ) -> tuple[str, str | None] | None:
        if sender is None:
            return None
        dispatched = self._dispatched_parties(claim_id)
        with self.app.state.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, email, meta FROM parties WHERE claim_id = :claim_id "
                    "AND role = 'assessor' ORDER BY id"
                ),
                {"claim_id": claim_id},
            ).mappings()
            matches = []
            for row in rows:
                email = row["email"]
                if (
                    row["id"] not in dispatched
                    or not isinstance(email, str)
                    or email.strip().casefold() != sender
                ):
                    continue
                meta = _json(row["meta"])
                vendor_id = meta.get("vendor_id") if isinstance(meta, dict) else None
                matches.append(
                    (
                        str(row["id"]),
                        vendor_id if isinstance(vendor_id, str) else None,
                    )
                )
        return matches[0] if len(matches) == 1 else None

    def _already_received(self, claim_id: str, document_id: str) -> bool:
        return any(
            event["payload"].get("document_id") == document_id
            for event in self.cascade._events(
                claim_id, "assessment.report_received"
            )
        )

    def _unattributed(
        self, claim_id: str, document_id: str, sender: str | None
    ) -> None:
        self.cascade._review_once(
            claim_id,
            item_type="EXCEPTION",
            subtype="report_unattributed",
            identity={"document_id": document_id},
            payload={
                "facts": {"document_id": document_id, "sender": sender},
                "risk": "the assessor report cannot be safely linked to a dispatched firm",
                "recommendation": "identify the sending assessor before reprocessing",
                "resolution_schema": "EXCEPTION@1",
            },
        )

    def consume(self, event: Any) -> None:
        if (
            event.type != "document.extracted"
            or not isinstance(event.claim_id, str)
            or not isinstance(event.payload, dict)
            or event.payload.get("doc_type") != "assessor_report"
        ):
            return
        document_id = event.payload.get("document_id")
        if not isinstance(document_id, str) or self._already_received(
            event.claim_id, document_id
        ):
            return
        document = self._document(document_id)
        if document is None or document.get("claim_id") != event.claim_id:
            return
        sender = self._sender(document.get("source"))
        matched = self._match_party(event.claim_id, sender)
        if matched is None:
            self._unattributed(event.claim_id, document_id, sender)
            return
        assessor_party_id, vendor_id = matched
        self.cascade._emit(
            claim_id=event.claim_id,
            event_type="assessment.report_received",
            payload={
                "claim_id": event.claim_id,
                "document_id": document_id,
                "assessor_party_id": assessor_party_id,
                "vendor_id": vendor_id,
            },
            correlation_id=event.id or new_ulid(),
            actor=ACTOR,
        )
        self.selection.report_received(event.claim_id, document_id)


__all__ = ["AssessmentReport"]
