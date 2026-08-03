"""Governed, durable Microsoft Graph outbound release transport."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from claim_core import new_ulid
from graph_integration.models import GraphOutboundRateBucket, GraphOutboundRelease

SIMPLE_ATTACHMENT_LIMIT = 3 * 1024 * 1024
UPLOAD_CHUNK_SIZE = 4 * 1024 * 1024
MESSAGES_PER_MINUTE = 30
BLOCKED = {"status": "blocked_on_inputs", "blocked_on": "graph_credentials"}


class GraphOutbound:
    """Queue executor installed behind CommunicationsService.execute_or_stage."""

    def __init__(self, app: Any, client: Any, config: dict[str, Any], ready: bool) -> None:
        self.app = app
        self.graph_client = client
        self.config = config
        self.ready = ready
        engine = getattr(app.state, "engine", None)
        self.sessions = sessionmaker(bind=engine, expire_on_commit=False) if engine else None

    def enqueue(self, action: Any) -> None:
        """Persist stable release intent before any Graph operation."""

        if self.sessions is None:
            return
        payload = dict(action.payload)
        now = self._now()
        write_id = str(payload["write_id"])
        with self.sessions.begin() as session:
            existing = session.scalar(
                select(GraphOutboundRelease).where(GraphOutboundRelease.write_id == write_id)
            )
            if existing is not None:
                return
            session.add(
                GraphOutboundRelease(
                    id=new_ulid(),
                    write_id=write_id,
                    claim_id=str(payload["claim_id"]),
                    action_payload=payload,
                    status="pending",
                    release_due_at=self._as_datetime(payload["release_due_at"]),
                    upload_offset=0,
                    attempts=0,
                    created_at=now,
                    updated_at=now,
                )
            )

    def release_due(self, now: datetime) -> dict[str, Any]:
        """Release at most the remaining durable 30-message/minute allowance."""

        if not self.ready or self.graph_client is None:
            return dict(BLOCKED)
        if self.sessions is None:
            return {"status": "completed", "released": 0}
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        with self.sessions.begin() as session:
            bucket = session.scalar(
                select(GraphOutboundRateBucket)
                .where(GraphOutboundRateBucket.bucket_id == "global")
                .with_for_update()
            )
            if bucket is None:
                bucket = GraphOutboundRateBucket(
                    bucket_id="global", window_started_at=now, used=0
                )
                session.add(bucket)
                session.flush()
            window_started_at = bucket.window_started_at
            if window_started_at.tzinfo is None:
                window_started_at = window_started_at.replace(tzinfo=UTC)
            if now - window_started_at >= timedelta(minutes=1):
                bucket.window_started_at = now
                bucket.used = 0
            allowance = max(0, MESSAGES_PER_MINUTE - bucket.used)
            rows = list(
                session.scalars(
                    select(GraphOutboundRelease)
                    .where(
                        GraphOutboundRelease.status.in_(
                            ("pending", "uncertain", "releasing")
                        ),
                        GraphOutboundRelease.release_due_at <= now,
                    )
                    .order_by(GraphOutboundRelease.release_due_at, GraphOutboundRelease.id)
                    .limit(allowance)
                )
            )
            for row in rows:
                row.status = "releasing"
                row.updated_at = now
            bucket.used += len(rows)
        released = 0
        exceptions = 0
        for row in rows:
            outcome = self._release_one(row.id, now)
            released += outcome == "sent"
            exceptions += outcome == "exception"
        return {
            "status": "completed",
            "released": released,
            "exceptions": exceptions,
        }

    def _release_one(self, release_id: str, now: datetime) -> str:
        with self.sessions() as session:
            release = session.get(GraphOutboundRelease, release_id)
            if release is None or release.status not in {
                "pending",
                "uncertain",
                "releasing",
            }:
                return "skipped"
            if release.status != "pending":
                probe = self.graph_client.probe_write(release.write_id)
                if probe.get("status") == "acknowledged":
                    self._record_sent(
                        release.id,
                        str(probe.get("graph_message_id", "probe-acknowledged")),
                        now,
                    )
                    return "sent"
                if probe.get("status") != "not_found":
                    return self._record_uncertain(release.id, now)
            payload = dict(release.action_payload)
            write_id = release.write_id

        with self.sessions.begin() as session:
            current = session.get(GraphOutboundRelease, release_id)
            if current is None:
                return "skipped"
            current.status = "uncertain"
            current.attempts += 1
            current.updated_at = now

        try:
            message = self._message(payload, release_id)
            result = self.graph_client.send_message(write_id, message)
        except Exception:
            probe = self.graph_client.probe_write(write_id)
            if probe.get("status") == "not_found":
                with self.sessions.begin() as session:
                    current = session.get(GraphOutboundRelease, release_id)
                    if current is not None:
                        current.status = "pending"
                        current.updated_at = now
                return "retryable"
            return self._record_uncertain(release_id, now)
        if result.get("status") != "acknowledged":
            return self._record_uncertain(release_id, now)
        self._record_sent(release_id, str(result["graph_message_id"]), now)
        return "sent"

    def _message(self, payload: dict[str, Any], release_id: str) -> dict[str, Any]:
        claim_id = str(payload["claim_id"])
        party_ids = [str(value) for value in payload.get("to_party_ids", [])]
        attachments = [str(value) for value in payload.get("attachments", [])]
        with self.app.state.engine.connect() as connection:
            party_rows = list(
                connection.execute(
                    text("SELECT id, email FROM parties WHERE claim_id = :claim_id"),
                    {"claim_id": claim_id},
                ).mappings()
            )
            addresses = [
                str(row["email"])
                for row in party_rows
                if str(row["id"]) in party_ids and isinstance(row["email"], str)
            ]
            documents = list(
                connection.execute(
                    text(
                        "SELECT id, filename, mime, s3_key FROM documents "
                        "WHERE claim_id = :claim_id"
                    ),
                    {"claim_id": claim_id},
                ).mappings()
            )
        selected = {str(row["id"]): row for row in documents if str(row["id"]) in attachments}
        simple: list[dict[str, Any]] = []
        uploaded: list[dict[str, str]] = []
        for attachment_id in attachments:
            row = selected[attachment_id]
            content = self.app.state.blob_store.get(str(row["s3_key"]))
            if len(content) <= SIMPLE_ATTACHMENT_LIMIT:
                simple.append(
                    {"name": str(row["filename"]), "mime": str(row["mime"]), "content": content}
                )
            else:
                self._upload_large(release_id, attachment_id, content)
                uploaded.append({"attachment_id": attachment_id})
        body_key = payload.get("blob_key")
        body = self.app.state.blob_store.get(body_key) if isinstance(body_key, str) else b""
        return {
            "to": addresses,
            "subject": str(payload.get("template_id", "Pacha claim update")),
            "body": body,
            "attachments": simple,
            "uploaded": uploaded,
        }

    def _upload_large(self, release_id: str, attachment_id: str, content: bytes) -> None:
        with self.sessions() as session:
            release = session.get(GraphOutboundRelease, release_id)
            if release is None:
                raise RuntimeError("release disappeared")
            session_ref = release.upload_session
            offset = release.upload_offset if release.upload_attachment_id == attachment_id else 0
            write_id = release.write_id
        if not session_ref or offset == 0:
            created = self.graph_client.create_upload_session(
                write_id, attachment_id, len(content)
            )
            session_ref = str(created["session"])
            offset = int(created.get("offset", 0))
            self._checkpoint(release_id, attachment_id, session_ref, offset)
        while offset < len(content):
            chunk = content[offset : offset + UPLOAD_CHUNK_SIZE]
            result = self.graph_client.upload_chunk(session_ref, offset, chunk, len(content))
            offset = int(result["offset"])
            self._checkpoint(release_id, attachment_id, session_ref, offset)

    def _checkpoint(
        self, release_id: str, attachment_id: str, session_ref: str, offset: int
    ) -> None:
        with self.sessions.begin() as session:
            release = session.get(GraphOutboundRelease, release_id)
            if release is not None:
                release.upload_session = session_ref
                release.upload_attachment_id = attachment_id
                release.upload_offset = offset
                release.updated_at = self._now()

    def _record_sent(self, release_id: str, graph_message_id: str, now: datetime) -> None:
        with self.sessions.begin() as session:
            release = session.get(GraphOutboundRelease, release_id)
            if release is None or release.status == "sent":
                return
            payload = dict(release.action_payload)
            communication_id = new_ulid()
            session.execute(
                text(
                    "INSERT INTO communications "
                    "(id, claim_id, direction, channel, graph_message_id, body_s3_key, "
                    "sent_by, occurred_at) VALUES "
                    "(:id, :claim_id, 'outbound', 'email', :message_id, :body_key, "
                    ":actor, :occurred_at)"
                ),
                {
                    "id": communication_id,
                    "claim_id": release.claim_id,
                    "message_id": graph_message_id,
                    "body_key": str(payload.get("blob_key", "graph/outbound/no-body")),
                    "actor": str(payload.get("actor", "system:graph")),
                    "occurred_at": now,
                },
            )
            self.app.state.record_event(
                session,
                claim_id=release.claim_id,
                event_type="email.sent",
                payload={
                    "communication_id": communication_id,
                    "graph_message_id": graph_message_id,
                    "write_id": release.write_id,
                },
                actor=str(payload.get("actor", "system:graph")),
                correlation_id=release.write_id,
            )
            release.status = "sent"
            release.graph_message_id = graph_message_id
            release.updated_at = now

    def _record_uncertain(self, release_id: str, now: datetime) -> str:
        with self.sessions.begin() as session:
            release = session.get(GraphOutboundRelease, release_id)
            if release is None:
                return "skipped"
            review_id = new_ulid()
            self.app.state.record_event(
                session,
                claim_id=release.claim_id,
                event_type="review.created",
                payload={
                    "review_id": review_id,
                    "type": "EXCEPTION",
                    "subtype": "uncertain_write",
                    "write_id": release.write_id,
                },
                actor="system:graph",
                correlation_id=release.write_id,
            )
            release.status = "exception"
            release.updated_at = now
        return "exception"

    def _now(self) -> datetime:
        clock = getattr(self.app.state, "clock", None)
        return clock() if callable(clock) else datetime.now(UTC)

    @staticmethod
    def _as_datetime(value: Any) -> datetime:
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value))


__all__ = ["GraphOutbound"]
