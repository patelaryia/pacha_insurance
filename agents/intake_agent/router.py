"""Strict-order PRD-05 §5.2 shared-mailbox router."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from claim_core import ClaimCoreError, new_ulid
from doc_intel.validators import kenya_reg
from intake_agent.classifier import CLASSES

TERMINAL_INBOUND_STATES = frozenset(
    {"DECLINED", "WITHDRAWN", "VOID", "SETTLED", "CLOSED"}
)
OPEN_REFERENCE_EXCLUSIONS = TERMINAL_INBOUND_STATES


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


class EmailRouter:
    """Route every non-self inbound exactly once and never choose ambiguously."""

    def __init__(self, app: Any, classifier: Any, config: dict[str, Any]) -> None:
        self.app = app
        self.classifier = classifier
        self.config = config
        self.sessions = sessionmaker(bind=app.state.engine, expire_on_commit=False)
        self.self_addresses = {
            value.strip().casefold() for value in config["self_addresses"]
        }

    def _emit(
        self,
        *,
        event_type: str,
        payload: dict[str, Any],
        claim_id: str | None,
        actor: str = "agent:intake",
        correlation_id: str | None = None,
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

    def _review(
        self,
        *,
        item_type: str,
        subtype: str,
        payload: dict[str, Any],
        claim_id: str | None,
        correlation_id: str,
    ) -> str:
        review_id = new_ulid()
        self._emit(
            event_type="review.created",
            payload={
                "review_id": review_id,
                "type": item_type,
                "subtype": subtype,
                **payload,
            },
            claim_id=claim_id,
            correlation_id=correlation_id,
        )
        return review_id

    def _already_seen(self, graph_message_id: str) -> bool:
        with self.app.state.engine.connect() as connection:
            return (
                connection.execute(
                    text(
                        "SELECT 1 FROM communications WHERE graph_message_id = :id LIMIT 1"
                    ),
                    {"id": graph_message_id},
                ).scalar()
                is not None
            )

    def _thread_candidates(self, conversation_id: str | None) -> list[str]:
        if not conversation_id:
            return []
        with self.app.state.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT DISTINCT claim_id FROM communications "
                    "WHERE thread_id = :thread_id AND claim_id IS NOT NULL ORDER BY claim_id"
                ),
                {"thread_id": conversation_id},
            ).scalars()
            return [str(value) for value in rows]

    def _reference_candidates(self, message: dict[str, Any]) -> list[str]:
        raw_text = f"{message['subject']}\n{message['body_text']}".upper()
        normalised_text = " ".join(raw_text.split())
        compact_text = normalised_text.replace(" ", "")
        with self.app.state.engine.connect() as connection:
            claims = list(
                connection.execute(
                    text(
                        "SELECT id FROM claims WHERE status NOT IN "
                        "('DECLINED','WITHDRAWN','VOID','SETTLED','CLOSED') ORDER BY id"
                    )
                ).scalars()
            )
            fields = connection.execute(
                text(
                    "SELECT claim_id, path, value FROM claim_fields "
                    "WHERE superseded_by IS NULL "
                    "AND path IN ('vehicle.reg', 'external.icon.claim_no')"
                )
            ).mappings()
            by_claim: dict[str, list[tuple[str, Any]]] = {}
            for row in fields:
                by_claim.setdefault(str(row["claim_id"]), []).append(
                    (str(row["path"]), _json_value(row["value"]))
                )
        matched: list[str] = []
        for raw_claim_id in claims:
            claim_id = str(raw_claim_id)
            if claim_id.upper() in normalised_text:
                matched.append(claim_id)
                continue
            found = False
            for path, value in by_claim.get(claim_id, []):
                if not isinstance(value, str) or not value.strip():
                    continue
                if path == "vehicle.reg":
                    validated = kenya_reg(value)
                    if validated.outcome != "pass" or not isinstance(validated.value, str):
                        continue
                    candidate = " ".join(validated.value.upper().split())
                    found = (
                        candidate in normalised_text
                        or candidate.replace(" ", "") in compact_text
                    )
                else:
                    found = value.strip().upper() in normalised_text
                if found:
                    break
            if found:
                matched.append(claim_id)
        return matched

    def _record_message(self, message: dict[str, Any], claim_id: str | None) -> bool:
        _row, created = self.app.state.claim_service.record_inbound_communication(
            graph_message_id=message["graph_message_id"],
            claim_id=claim_id,
            thread_id=message.get("conversation_id"),
            from_addr=message["from_addr"],
            to_addrs=list(message["to_addrs"]),
            subject=message["subject"],
            body_text=message["body_text"],
        )
        return created

    def _attach(self, message: dict[str, Any], claim_id: str, *, reference: bool) -> None:
        if not self._record_message(message, claim_id):
            return
        graph_message_id = message["graph_message_id"]
        for index, attachment in enumerate(message["attachments"]):
            try:
                content = base64.b64decode(attachment["content_b64"], validate=True)
                self.app.state.claim_service.add_document(
                    claim_id,
                    filename=attachment["filename"],
                    mime=attachment["mime"],
                    content=content,
                    source_channel="email",
                    source_ref=f"{graph_message_id}:{index}",
                    actor="agent:intake",
                )
            except ClaimCoreError as error:
                if error.code == "DUPLICATE_DOCUMENT":
                    self._emit(
                        event_type="INBOUND_DUPLICATE_ATTACHMENT",
                        payload={
                            "graph_message_id": graph_message_id,
                            "attachment_index": index,
                            "filename": attachment.get("filename"),
                            "sha256": hashlib.sha256(content).hexdigest(),
                            "outcome": "existing_document_retained",
                        },
                        claim_id=claim_id,
                        correlation_id=graph_message_id,
                    )
                    continue
                self._review(
                    item_type="EXCEPTION",
                    subtype="inbound_attachment_invalid",
                    payload={
                        "graph_message_id": graph_message_id,
                        "attachment_index": index,
                        "error_type": type(error).__name__,
                        "code": error.code,
                    },
                    claim_id=claim_id,
                    correlation_id=graph_message_id,
                )
            except (KeyError, TypeError, ValueError) as error:
                self._review(
                    item_type="EXCEPTION",
                    subtype="inbound_attachment_invalid",
                    payload={
                        "graph_message_id": graph_message_id,
                        "attachment_index": index,
                        "error_type": type(error).__name__,
                    },
                    claim_id=claim_id,
                    correlation_id=graph_message_id,
                )
        status = self._claim_status(claim_id)
        terminal = status in TERMINAL_INBOUND_STATES
        if reference or terminal:
            self._emit(
                event_type="INBOUND_ATTACHED",
                payload={
                    "graph_message_id": graph_message_id,
                    "match": "reference" if reference else "thread",
                    "terminal": terminal,
                },
                claim_id=claim_id,
                correlation_id=graph_message_id,
            )
        if terminal:
            self._emit(
                event_type="terminal.inbound_received",
                payload={
                    "graph_message_id": graph_message_id,
                    "claim_status": status,
                    "has_attachments": bool(message["attachments"]),
                },
                claim_id=claim_id,
                correlation_id=graph_message_id,
            )
            if status in {"DECLINED", "WITHDRAWN"} and message["attachments"]:
                self._review(
                    item_type="REOPEN_PROMPT",
                    subtype="attachment_bearing_inbound",
                    payload={
                        "graph_message_id": graph_message_id,
                        "claim_status": status,
                    },
                    claim_id=claim_id,
                    correlation_id=graph_message_id,
                )

    def _claim_status(self, claim_id: str) -> str | None:
        with self.app.state.engine.connect() as connection:
            value = connection.execute(
                text("SELECT status FROM claims WHERE id = :id"), {"id": claim_id}
            ).scalar()
        return value if isinstance(value, str) else None

    @staticmethod
    def _sampled(graph_message_id: str, rate: int) -> bool:
        bucket = int(hashlib.sha256(graph_message_id.encode("utf-8")).hexdigest()[:8], 16) % 100
        return bucket < rate

    def _classify(self, message: dict[str, Any]) -> None:
        graph_message_id = message["graph_message_id"]
        self._record_message(message, None)
        try:
            result = self.classifier.classify(message)
        except Exception as error:  # noqa: BLE001 - classifier failure is visible work
            self._review(
                item_type="EXCEPTION",
                subtype="mailbox_classifier_failed",
                payload={
                    "graph_message_id": graph_message_id,
                    "error_type": type(error).__name__,
                },
                claim_id=None,
                correlation_id=graph_message_id,
            )
            return
        class_name = result.get("class")
        confidence = result.get("confidence")
        if (
            class_name not in CLASSES
            or not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not 0 <= confidence <= 1
        ):
            self._review(
                item_type="EXCEPTION",
                subtype="mailbox_classifier_schema_invalid",
                payload={"graph_message_id": graph_message_id},
                claim_id=None,
                correlation_id=graph_message_id,
            )
            return
        thresholds = self.config["classifier"]["thresholds"]
        classification = {
            "graph_message_id": graph_message_id,
            "class": class_name,
            "confidence": confidence,
        }
        if class_name == "new_intimation" and confidence >= thresholds["new_intimation"]:
            self._emit(
                event_type="intake.requested",
                payload={**classification, "message": message},
                claim_id=None,
                correlation_id=graph_message_id,
            )
            return
        if class_name == "multi_intimation":
            self._review(
                item_type="EXCEPTION",
                subtype="multi_claim_email",
                payload=classification,
                claim_id=None,
                correlation_id=graph_message_id,
            )
            return
        if class_name == "not_a_claim" and confidence >= thresholds["not_a_claim"]:
            self._emit(
                event_type="mail.archived",
                payload={**classification, "transport_status": "pending_capture"},
                claim_id=None,
                correlation_id=graph_message_id,
            )
            rate = self.config["archive_sample_rate"]
            if self._sampled(graph_message_id, rate):
                self._review(
                    item_type="SAMPLE_REVIEW",
                    subtype="mailbox_archive",
                    payload={**classification, "already_executed": True},
                    claim_id=None,
                    correlation_id=graph_message_id,
                )
            return
        self._review(
            item_type="DOC_CLASSIFY",
            subtype="mailbox_triage",
            payload=classification,
            claim_id=None,
            correlation_id=graph_message_id,
        )

    def consume(self, event: Any) -> None:
        if event.type != "email.received" or not isinstance(event.payload, dict):
            return
        message = dict(event.payload)
        required = {
            "graph_message_id",
            "conversation_id",
            "from_addr",
            "to_addrs",
            "subject",
            "body_text",
            "attachments",
        }
        if not required <= set(message) or not isinstance(message["graph_message_id"], str):
            self._review(
                item_type="EXCEPTION",
                subtype="inbound_payload_invalid",
                payload={"source_event_id": event.id},
                claim_id=None,
                correlation_id=event.id,
            )
            return
        if str(message["from_addr"]).strip().casefold() in self.self_addresses:
            return
        graph_message_id = message["graph_message_id"]
        if self._already_seen(graph_message_id):
            return
        thread_matches = self._thread_candidates(message.get("conversation_id"))
        if len(thread_matches) == 1:
            self._attach(message, thread_matches[0], reference=False)
            return
        if len(thread_matches) > 1:
            self._record_message(message, None)
            self._review(
                item_type="EXCEPTION",
                subtype="ambiguous_inbound",
                payload={"graph_message_id": graph_message_id, "candidates": thread_matches},
                claim_id=None,
                correlation_id=graph_message_id,
            )
            return
        reference_matches = self._reference_candidates(message)
        if len(reference_matches) == 1:
            self._attach(message, reference_matches[0], reference=True)
            return
        if len(reference_matches) > 1:
            self._record_message(message, None)
            self._review(
                item_type="EXCEPTION",
                subtype="ambiguous_inbound",
                payload={
                    "graph_message_id": graph_message_id,
                    "candidates": reference_matches,
                },
                claim_id=None,
                correlation_id=graph_message_id,
            )
            return
        self._classify(message)


__all__ = ["EmailRouter"]
