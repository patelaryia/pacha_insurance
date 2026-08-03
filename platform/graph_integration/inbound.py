"""Atomic, idempotent Microsoft Graph inbound delta ingestion."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from claim_core import new_ulid
from graph_integration.models import GraphBinding, GraphInboundReceipt

MAX_MESSAGE_BYTES = 25 * 1024 * 1024
BLOCKED = {"status": "blocked_on_inputs", "blocked_on": "graph_credentials"}


class GraphInbound:
    def __init__(self, app: Any, client: Any, config: dict[str, Any], ready: bool) -> None:
        self.app = app
        self.graph_client = client
        self.config = config
        self.ready = ready
        self.mailbox_ref = str(config.get("mailbox_ref", "injected-test-mailbox"))
        engine = getattr(app.state, "engine", None)
        self.sessions = sessionmaker(bind=engine, expire_on_commit=False) if engine else None
        self._memory: dict[str, Any] = {}

    def _binding(self, session: Any) -> GraphBinding:
        row = session.get(GraphBinding, self.mailbox_ref)
        if row is None:
            row = GraphBinding(mailbox_ref=self.mailbox_ref)
            session.add(row)
            session.flush()
        return row

    def _normalise(self, message: dict[str, Any]) -> dict[str, Any]:
        message_id = str(message["id"])
        attachments: list[dict[str, Any]] = []
        for item in message.get("attachments", []):
            attachment_id = str(item["id"])
            content = self.graph_client.download_attachment(message_id, attachment_id)
            attachments.append(
                {
                    "filename": str(item.get("name", "attachment")),
                    "mime": str(item.get("content_type", "application/octet-stream")),
                    "content_b64": base64.b64encode(content).decode("ascii"),
                }
            )
        return {
            "graph_message_id": message_id,
            "conversation_id": message.get("conversation_id"),
            "from_addr": str(message.get("from_addr", "")),
            "to_addrs": [str(value) for value in message.get("to_addrs", [])],
            "subject": str(message.get("subject", "")),
            "body_text": str(message.get("body", "")),
            "attachments": attachments,
        }

    def delta_once(self) -> dict[str, Any]:
        if not self.ready:
            return dict(BLOCKED)
        if self.sessions is None:
            page = self.graph_client.delta_page(self._memory.get("delta_token"))
            self._memory["delta_token"] = page.get("next_token")
            return {"status": "completed", "received": len(page.get("messages", []))}

        with self.sessions() as session:
            token = session.get(GraphBinding, self.mailbox_ref)
            delta_token = token.delta_token if token is not None else None
        page = self.graph_client.delta_page(delta_token)
        received = 0
        try:
            with self.sessions.begin() as session:
                binding = self._binding(session)
                for message in page.get("messages", []):
                    message_id = str(message["id"])
                    if session.get(GraphInboundReceipt, message_id) is not None:
                        continue
                    size = int(message.get("size", 0))
                    if size > MAX_MESSAGE_BYTES:
                        review_id = new_ulid()
                        event = self.app.state.record_event(
                            session,
                            claim_id=None,
                            event_type="review.created",
                            payload={
                                "review_id": review_id,
                                "type": "EXCEPTION",
                                "subtype": "inbound_message_too_large",
                                "graph_message_id": message_id,
                            },
                            actor="system:graph",
                            correlation_id=review_id,
                        )
                    else:
                        event = self.app.state.record_event(
                            session,
                            claim_id=None,
                            event_type="email.received",
                            payload=self._normalise(message),
                            actor="system:graph",
                            correlation_id=message_id,
                        )
                    session.add(
                        GraphInboundReceipt(
                            graph_message_id=message_id,
                            event_id=event.id,
                        )
                    )
                    received += 1
                binding.delta_token = page.get("next_token")
                binding.last_successful_poll_at = datetime.now(UTC)
        except IntegrityError:
            return {"status": "retryable", "received": 0}
        return {"status": "completed", "received": received}

    def renew_once(self) -> dict[str, Any]:
        if not self.ready:
            return dict(BLOCKED)
        raw_state = secrets.token_urlsafe(32)
        result = self.graph_client.renew_subscription(raw_state)
        digest = hashlib.sha256(raw_state.encode("utf-8")).hexdigest()
        if self.sessions is None:
            self._memory.update(result)
            self._memory["client_state_sha256"] = digest
            return {"status": "completed"}
        with self.sessions.begin() as session:
            binding = self._binding(session)
            binding.subscription_id = str(result["subscription_id"])
            binding.subscription_expires_at = datetime.fromisoformat(result["expires_at"])
            binding.client_state_sha256 = digest
        return {"status": "completed"}

    def accept_webhook(self, client_state: str) -> dict[str, Any]:
        digest = hashlib.sha256(client_state.encode("utf-8")).hexdigest()
        if self.sessions is None:
            expected = self._memory.get("client_state_sha256")
        else:
            with self.sessions() as session:
                binding = session.get(GraphBinding, self.mailbox_ref)
                expected = binding.client_state_sha256 if binding else None
        if not isinstance(expected, str) or not hmac.compare_digest(digest, expected):
            return {"status": "refused"}
        return {"status": "poll_requested"}
