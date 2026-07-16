"""PRD-05 §5.3 S1–S7 durable intake COP flow."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from agent_runtime import Action
from claim_core import ClaimCoreError, ClaimCreate, FieldWrite, new_ulid

ACTOR = "agent:intake"
CAPABILITY = "intake.claim_creation"


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


class IntakeFlow:
    """Consume ``intake.requested`` and drive one idempotent S1–S8 run."""

    def __init__(self, app: Any, config: dict[str, Any], triage: Any) -> None:
        self.app = app
        self.config = config
        self.triage = triage
        self.sessions = sessionmaker(bind=app.state.engine, expire_on_commit=False)
        pack_path = Path(__file__).resolve().parents[2] / "packs" / "motor" / "pack.yaml"
        pack = yaml.safe_load(pack_path.read_text(encoding="utf-8"))
        self.pack_version = f"{pack['id']}@{pack['version']}"
        app.state.agent_runtime.register_executor("intake.create_claim", self._create_claim)
        for step_id, fn in (
            ("create_claim", self.create_claim),
            ("ingest", self.ingest),
            ("populate", self.populate),
            ("dupe_check", self.dupe_check),
            ("late_check", self.late_check),
            ("acknowledge", self.acknowledge),
            ("checklist", self.checklist),
            ("triage", self.triage.step),
        ):
            app.state.agent_runtime.register_step(CAPABILITY, step_id, fn)

    def _event(self, event_id: str) -> dict[str, Any]:
        with self.app.state.engine.connect() as connection:
            row = connection.execute(
                text("SELECT payload, occurred_at FROM events WHERE id = :id"),
                {"id": event_id},
            ).first()
        if row is None:
            raise LookupError(f"trigger event {event_id} was not found")
        payload = _json_value(row[0])
        if not isinstance(payload, dict):
            raise ValueError("trigger payload must be a mapping")
        return {"payload": payload, "occurred_at": row[1]}

    def _message(self, context: Any) -> dict[str, Any]:
        if not isinstance(context.trigger_event, str):
            raise ValueError("intake run requires a trigger event")
        message = self._event(context.trigger_event)["payload"].get("message")
        if not isinstance(message, dict):
            raise ValueError("intake.requested requires the routed message")
        return message

    def _emit(
        self,
        *,
        claim_id: str | None,
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

    def _review(
        self,
        context: Any,
        *,
        item_type: str,
        subtype: str,
        payload: dict[str, Any],
    ) -> str:
        review_id = new_ulid()
        self._emit(
            claim_id=context.claim_id,
            event_type="review.created",
            payload={
                "review_id": review_id,
                "type": item_type,
                "subtype": subtype,
                "agent_run_id": context.run_id,
                "capability_id": context.capability_id,
                **payload,
            },
            correlation_id=context.run_id,
        )
        return review_id

    def _timeline_event(
        self,
        claim_id: str,
        event_type: str,
        predicate: Any,
    ) -> Any | None:
        return next(
            (
                event
                for event in self.app.state.claim_service.timeline(claim_id)
                if event.type == event_type and predicate(event.payload)
            ),
            None,
        )

    def consume(self, event: Any) -> None:
        if event.type != "intake.requested":
            return
        with self.app.state.engine.connect() as connection:
            existing = connection.execute(
                text(
                    "SELECT id FROM agent_runs WHERE trigger_event = :event_id "
                    "AND capability_id = :capability_id ORDER BY started_at, id LIMIT 1"
                ),
                {"event_id": event.id, "capability_id": CAPABILITY},
            ).scalar()
        if isinstance(existing, str):
            return
        run_id = self.app.state.agent_runtime.start_run(
            agent="intake",
            capability_id=CAPABILITY,
            trigger_event=event.id,
        )
        self.app.state.agent_runtime.run(run_id)

    def _create_claim(self, action: Action) -> str:
        run_id = action.payload.get("workflow_run_id")
        if not isinstance(run_id, str):
            raise ValueError("intake.create_claim requires workflow_run_id")
        with self.app.state.engine.connect() as connection:
            existing = connection.execute(
                text("SELECT claim_id FROM agent_runs WHERE id = :id"), {"id": run_id}
            ).scalar()
        if isinstance(existing, str):
            return existing
        claim = self.app.state.claim_service.create_claim(
            ClaimCreate(lob="motor", pack_version=self.pack_version),
            actor=ACTOR,
        )
        self.app.state.agent_runtime.runner.set_claim_id(run_id, claim.id)
        return claim.id

    def create_claim(self, context: Any) -> dict[str, Any]:
        if context.claim_id is not None:
            return {"status": "completed", "claim_id": context.claim_id}
        message = self._message(context)
        return self.app.state.agent_runtime.execute_or_stage(
            capability_id=CAPABILITY,
            action=Action(
                type="intake.create_claim",
                payload={
                    "workflow_run_id": context.run_id,
                    "graph_message_id": message["graph_message_id"],
                    "lob": "motor",
                    "pack_version": self.pack_version,
                },
            ),
            claim_id=None,
            actor=ACTOR,
            run_id=context.run_id,
        )

    @staticmethod
    def _email_bytes(message: dict[str, Any]) -> bytes:
        email = EmailMessage()
        email["Subject"] = str(message["subject"])
        email["From"] = str(message["from_addr"])
        email["To"] = ", ".join(map(str, message["to_addrs"]))
        email.set_content(str(message["body_text"]))
        return email.as_bytes()

    def ingest(self, context: Any) -> dict[str, Any]:
        if context.claim_id is None:
            raise ValueError("ingest requires the governed claim creation to complete")
        message = self._message(context)
        graph_id = str(message["graph_message_id"])
        existing_refs = {
            document.source.get("source_ref")
            for document in self.app.state.claim_service.documents(context.claim_id)
        }
        body_ref = f"{graph_id}:body"
        if body_ref not in existing_refs:
            self.app.state.claim_service.add_document(
                context.claim_id,
                filename=f"intimation-{graph_id}.eml",
                mime="message/rfc822",
                content=self._email_bytes(message),
                source_channel="email",
                source_ref=body_ref,
                actor=ACTOR,
            )
            existing_refs.add(body_ref)
        for index, attachment in enumerate(message["attachments"]):
            source_ref = f"{graph_id}:{index}"
            if source_ref in existing_refs:
                continue
            content = base64.b64decode(attachment["content_b64"], validate=True)
            try:
                self.app.state.claim_service.add_document(
                    context.claim_id,
                    filename=attachment["filename"],
                    mime=attachment["mime"],
                    content=content,
                    source_channel="email",
                    source_ref=source_ref,
                    actor=ACTOR,
                )
            except ClaimCoreError as error:
                if error.code != "DUPLICATE_DOCUMENT":
                    raise
                self._emit(
                    claim_id=context.claim_id,
                    event_type="INBOUND_DUPLICATE_ATTACHMENT",
                    payload={
                        "graph_message_id": graph_id,
                        "attachment_index": index,
                        "filename": attachment.get("filename"),
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "outcome": "existing_document_retained",
                    },
                    correlation_id=context.run_id,
                )
        return {"status": "completed"}

    def _received_at(self, graph_message_id: str) -> Any:
        with self.app.state.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT payload, occurred_at FROM events "
                    "WHERE type = 'email.received' ORDER BY seq"
                )
            )
            for payload, occurred_at in rows:
                decoded = _json_value(payload)
                if (
                    isinstance(decoded, dict)
                    and decoded.get("graph_message_id") == graph_message_id
                ):
                    return occurred_at
        raise LookupError("source email.received event was not found")

    def populate(self, context: Any) -> dict[str, Any]:
        if context.claim_id is None:
            raise ValueError("populate requires a claim")
        message = self._message(context)
        body_ref = f"{message['graph_message_id']}:body"
        body_document = next(
            (
                document
                for document in self.app.state.claim_service.documents(context.claim_id)
                if document.source.get("source_ref") == body_ref
            ),
            None,
        )
        if body_document is None or body_document.status != "extracted":
            return {"status": "waiting", "expects_event": "document.extracted"}
        _claim, fields, _blocked = self.app.state.claim_service.hydrate_claim(
            context.claim_id,
            ACTOR,
            paths=["parties.insured.name", "intimation.received_at"],
        )
        parties = [
            {
                "role": "broker",
                "email": message["from_addr"],
                "meta": {"source": "intimation_sender"},
            }
        ]
        insured = fields.get("parties.insured.name")
        if insured is not None and isinstance(insured.value, str) and insured.value.strip():
            parties.append(
                {
                    "role": "insured",
                    "name": insured.value,
                    "meta": {"source": "extraction"},
                }
            )
        self.app.state.claim_service.create_parties(
            context.claim_id, parties, actor=ACTOR
        )
        if "intimation.received_at" not in fields:
            received_at = self._received_at(str(message["graph_message_id"]))
            if isinstance(received_at, str):
                parsed = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
            else:
                parsed = received_at
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            received_value = parsed.isoformat()
            self.app.state.claim_service.write_fields(
                context.claim_id,
                [
                    FieldWrite(
                        path="intimation.received_at",
                        value=received_value,
                        value_type="datetime",
                        source_type="system",
                        source_ref={"event_type": "email.received"},
                        verification_state="system_confirmed",
                    )
                ],
                ACTOR,
            )
        return {"status": "completed", "document_id": body_document.id}

    def dupe_check(self, context: Any) -> dict[str, Any]:
        if context.claim_id is None:
            raise ValueError("dupe_check requires a claim")
        _claim, fields, _blocked = self.app.state.claim_service.hydrate_claim(
            context.claim_id, ACTOR, paths=["vehicle.reg", "loss.date"]
        )
        reg = fields.get("vehicle.reg")
        loss = fields.get("loss.date")
        if (
            reg is None
            or loss is None
            or not isinstance(reg.value, str)
            or not isinstance(loss.value, str)
        ):
            return {"status": "insufficient", "missing": ["vehicle.reg", "loss.date"]}
        candidates = self.app.state.claim_service.find_claim_duplicates(
            claim_id=context.claim_id,
            vehicle_reg=reg.value,
            loss_date=loss.value,
        )
        open_ids = [row["claim_id"] for row in candidates if not row["terminal"]]
        if open_ids:
            review_id = self._review(
                context,
                item_type="EXCEPTION",
                subtype="possible_duplicate",
                payload={"candidates": open_ids},
            )
            return {"status": "awaiting_review", "review_id": review_id}
        for row in candidates:
            if not row["terminal"]:
                continue
            matched_id = str(row["claim_id"])
            existing = self._timeline_event(
                context.claim_id,
                "fraud.signal",
                lambda payload, matched_id=matched_id: payload.get("matched_claim_id")
                == matched_id,
            )
            if existing is None:
                self._emit(
                    claim_id=context.claim_id,
                    event_type="fraud.signal",
                    payload={
                        "claim_id": context.claim_id,
                        "matched_claim_id": matched_id,
                        "vehicle_reg": reg.value,
                        "loss_date": loss.value,
                        "matched_terminal_state": row["status"],
                    },
                    correlation_id=context.run_id,
                )
        return {"status": "completed", "fraud_signals": sum(row["terminal"] for row in candidates)}

    def late_check(self, context: Any) -> dict[str, Any]:
        if context.claim_id is None:
            raise ValueError("late_check requires a claim")
        with self.app.state.engine.connect() as connection:
            existing = connection.execute(
                text(
                    "SELECT status FROM rule_runs WHERE claim_id = :claim_id "
                    "AND rule_id = 'R-10' ORDER BY evaluated_at, id LIMIT 1"
                ),
                {"claim_id": context.claim_id},
            ).scalar()
        if isinstance(existing, str):
            return {"status": existing, "rule_id": "R-10"}
        result = self.app.state.cop_runtime.evaluate("R-10", context.claim_id, ACTOR)
        return {"status": result.status, "rule_id": result.rule_id}

    def acknowledge(self, context: Any) -> dict[str, Any]:
        if context.claim_id is None:
            raise ValueError("acknowledge requires a claim")
        existing = self._timeline_event(
            context.claim_id,
            "review.created",
            lambda payload: payload.get("agent_run_id") == context.run_id
            and payload.get("action", {}).get("payload", {}).get("template_id") == "T-06a",
        )
        if existing is not None:
            return {
                "status": "staged",
                "review_id": existing.payload.get("review_id"),
            }
        message = self._message(context)
        with self.app.state.engine.connect() as connection:
            party_id = connection.execute(
                text(
                    "SELECT id FROM parties WHERE claim_id = :claim_id AND email = :email "
                    "ORDER BY id LIMIT 1"
                ),
                {"claim_id": context.claim_id, "email": message["from_addr"]},
            ).scalar()
        if not isinstance(party_id, str):
            return {
                "status": "insufficient",
                "missing": ["intimation_sender_party"],
                "graph_message_id": message["graph_message_id"],
            }
        return self.app.state.agent_runtime.comms.send(
            template_id="T-06a",
            claim_id=context.claim_id,
            to_party_ids=[party_id],
            attachments=(),
            capability_id="intake.acknowledge",
            actor=ACTOR,
            run_id=context.run_id,
        )

    def checklist(self, context: Any) -> dict[str, Any]:
        if context.claim_id is None:
            raise ValueError("checklist requires a claim")
        existing = self._timeline_event(
            context.claim_id,
            "chase.init",
            lambda payload: payload.get("claim_id") == context.claim_id,
        )
        if existing is not None:
            return {"status": "completed", "event_id": existing.id}
        held_types = {
            document.doc_type
            for document in self.app.state.claim_service.documents(context.claim_id)
            if isinstance(document.doc_type, str)
        }
        items = [
            {
                "id": item_id,
                "already_received": bool(
                    set(self.config["checklist_doc_types"][item_id]) & held_types
                ),
            }
            for item_id in self.config["checklist_base_items"]
        ]
        _claim, fields, _blocked = self.app.state.claim_service.hydrate_claim(
            context.claim_id, ACTOR, paths=["loss.narrative"]
        )
        if "loss.narrative" not in fields:
            items.append({"id": "incident_description", "already_received": False})
        event_id = self._emit(
            claim_id=context.claim_id,
            event_type="chase.init",
            payload={"claim_id": context.claim_id, "items": items},
            correlation_id=context.run_id,
        )
        return {"status": "completed", "event_id": event_id}


class TerminalMoneyConsumer:
    """Keep the SETTLED/CLOSED money-document arm wired to captured pack data."""

    def __init__(self, app: Any, configured_types: Any) -> None:
        self.app = app
        self.configured_types = (
            frozenset(configured_types) if isinstance(configured_types, list) else frozenset()
        )
        self.sessions = sessionmaker(bind=app.state.engine, expire_on_commit=False)

    def consume(self, event: Any) -> None:
        if event.type != "document.classified" or event.claim_id is None:
            return
        doc_type = event.payload.get("doc_type")
        if doc_type not in self.configured_types:
            return
        claim, _fields, _blocked = self.app.state.claim_service.hydrate_claim(
            event.claim_id, ACTOR, paths=[]
        )
        if claim.status not in {"SETTLED", "CLOSED"}:
            return
        with self.sessions.begin() as session:
            self.app.state.record_event(
                session,
                claim_id=event.claim_id,
                event_type="review.created",
                payload={
                    "type": "EXCEPTION",
                    "subtype": "terminal_money_relevant_document",
                    "document_id": event.payload.get("document_id"),
                    "doc_type": doc_type,
                    "role": "claims_manager",
                },
                actor=ACTOR,
                correlation_id=event.id,
            )


__all__ = ["IntakeFlow", "TerminalMoneyConsumer"]
