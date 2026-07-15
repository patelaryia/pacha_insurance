"""AR-3 claim-party outbound communications service."""

from __future__ import annotations

from datetime import datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from sqlalchemy import text

from agent_runtime.gate import Action, ExecutionRefused
from claim_core import new_ulid
from cop_runtime.templates import TemplateRenderBlocked

EAT = ZoneInfo("Africa/Nairobi")


def _fixed_holidays(path: Path) -> frozenset[str]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"invalid send-window holiday calendar: {error}") from error
    values = payload.get("fixed_dates") if isinstance(payload, dict) else None
    if not isinstance(values, list) or not all(
        isinstance(value, str) and len(value) == 5 for value in values
    ):
        raise ValueError("send-window holidays require fixed_dates")
    return frozenset(values)


class CommunicationsService:
    """Render, grade, govern, and visibly block the pending Graph transport."""

    def __init__(self, app: Any, gate: Any, pack_root: Path) -> None:
        self.app = app
        self.gate = gate
        self.holidays = _fixed_holidays(pack_root / "sla" / "holidays.yaml")
        self.gate.register_executor("communication.send", self._transport)

    @staticmethod
    def _transport(_action: Action) -> None:
        raise ExecutionRefused("transport_pending_capture")

    def _definition(self, template_id: str, claim_id: str) -> Any:
        with self.app.state.engine.connect() as connection:
            pack_version = connection.execute(
                text("SELECT pack_version FROM claims WHERE id = :claim_id"),
                {"claim_id": claim_id},
            ).scalar()
        if not isinstance(pack_version, str):
            raise LookupError("claim or pinned pack was not found")
        pack_id, separator, version = pack_version.partition("@")
        if not separator:
            raise LookupError("claim pack pin is malformed")
        return self.app.state.cop_runtime.template_registry(pack_id, version).get(template_id)

    def _party_ids(self, claim_id: str) -> set[str]:
        with self.app.state.engine.connect() as connection:
            return {
                str(value)
                for value in connection.execute(
                    text("SELECT id FROM parties WHERE claim_id = :claim_id"),
                    {"claim_id": claim_id},
                ).scalars()
            }

    def _attachments_valid(self, claim_id: str, attachments: tuple[Any, ...]) -> bool:
        if not attachments:
            return True
        if not all(isinstance(value, str) for value in attachments):
            return False
        with self.app.state.engine.connect() as connection:
            found = {
                str(value)
                for value in connection.execute(
                    text("SELECT id FROM documents WHERE claim_id = :claim_id"),
                    {"claim_id": claim_id},
                ).scalars()
            }
        return set(attachments) <= found

    def _review_exception(
        self,
        *,
        claim_id: str,
        actor: str,
        subtype: str,
        payload: dict[str, Any],
    ) -> str:
        review_id = new_ulid()
        with self.gate.runner.sessions.begin() as session:
            self.app.state.record_event(
                session,
                claim_id=claim_id,
                event_type="review.created",
                payload={
                    "review_id": review_id,
                    "type": "EXCEPTION",
                    "subtype": subtype,
                    **payload,
                },
                actor=actor,
                correlation_id=review_id,
            )
        return review_id

    def _in_window(self, now: datetime) -> bool:
        local = now.astimezone(EAT)
        if local.weekday() == 6 or local.strftime("%m-%d") in self.holidays:
            return False
        return time(8, 0) <= local.time().replace(tzinfo=None) < time(18, 0)

    def send(
        self,
        *,
        template_id: str,
        claim_id: str,
        to_party_ids: list[str],
        attachments: tuple[Any, ...],
        capability_id: str,
        actor: str,
    ) -> dict[str, Any]:
        """Apply registration, G-COMM, send window, rendering, and AR-2."""

        try:
            definition = self._definition(template_id, claim_id)
        except LookupError:
            return {
                "status": "refused",
                "code": "TEMPLATE_NOT_REGISTERED",
                "review_id": None,
            }
        recipients = list(dict.fromkeys(to_party_ids))
        valid_parties = self._party_ids(claim_id)
        if (
            not recipients
            or set(recipients) - valid_parties
            or not self._attachments_valid(claim_id, attachments)
        ):
            review_id = self._review_exception(
                claim_id=claim_id,
                actor=actor,
                subtype="g_comm_failed",
                payload={
                    "template_id": template_id,
                    "to_party_ids": recipients,
                    "reason": "recipient_or_attachment_outside_claim",
                },
            )
            return {
                "status": "refused",
                "code": "G_COMM_FAILED",
                "review_id": review_id,
            }
        if (
            capability_id not in self.gate.config["exempt_capabilities"]
            and not self._in_window(self.app.state.clock())
        ):
            return {"status": "queued_window", "code": None, "review_id": None}

        payload: dict[str, Any] = {
            "template_id": template_id,
            "to_party_ids": recipients,
            "attachments": list(attachments),
            "template_status": definition.status,
        }
        if definition.status == "pending_capture":
            payload.update(
                {
                    "body": "pending_capture",
                    "blocked_on": list(definition.blocked_on),
                    "signable": False,
                }
            )
        else:
            try:
                rendered = self.app.state.cop_runtime.render(template_id, claim_id, actor)
            except TemplateRenderBlocked as error:
                review_id = self._review_exception(
                    claim_id=claim_id,
                    actor=actor,
                    subtype="g_comm_failed",
                    payload={
                        "template_id": template_id,
                        "to_party_ids": recipients,
                        "reason": error.reason,
                        "missing_fields": error.missing_fields,
                        "under_verified": error.under_verified,
                    },
                )
                return {
                    "status": "refused",
                    "code": "G_COMM_FAILED",
                    "review_id": review_id,
                }
            payload.update(
                {
                    "blob_key": rendered.blob_key,
                    "signable": rendered.signable,
                    "placeholders_pending": rendered.placeholders_pending,
                }
            )
        outcome = self.gate.execute_or_stage(
            capability_id=capability_id,
            action=Action(
                type="communication.send",
                payload=payload,
                grader_id="G-COMM",
            ),
            claim_id=claim_id,
            actor=actor,
        )
        return {
            "status": outcome["status"],
            "code": None,
            "review_id": outcome.get("review_id"),
        }


__all__ = ["CommunicationsService"]
