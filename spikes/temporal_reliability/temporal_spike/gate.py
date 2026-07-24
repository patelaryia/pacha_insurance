"""Spike-only AR-2 gate proving uncertain-write and idempotency behaviour."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from temporalio.exceptions import ApplicationError

from .contracts import ExternalActionCommand
from .store import AuthoritativeStore


@dataclass
class SyntheticExternalSystem:
    """Target stub. `uncertain` means submit happened but receipt was lost."""

    mode: str = "success"

    def perform(self, payload: dict[str, Any], write_id: str) -> str:
        if self.mode == "uncertain":
            raise TimeoutError("receipt lost after possible target write")
        return f"receipt:{write_id}:{len(payload)}"


def execute_or_stage(
    command: ExternalActionCommand,
    *,
    store: AuthoritativeStore,
    external_system: SyntheticExternalSystem,
) -> str:
    """The sole external-action choke point in the spike."""

    existing = store.external_action(command.write_id)
    if existing is not None:
        if existing["payload_hash"] != command.payload_hash:
            raise ApplicationError(
                "idempotency payload mismatch",
                type="idempotency_conflict",
                non_retryable=True,
            )
        if existing["status"] == "completed":
            return str(existing["receipt_ref"])
        if existing["status"] == "uncertain":
            raise ApplicationError(
                "uncertain external write requires human verification",
                type="uncertain_write",
                non_retryable=True,
            )

    payload = store.target_payload(command.claim_ref)
    actual_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if actual_hash != command.payload_hash:
        raise ApplicationError(
            "authoritative payload hash mismatch",
            type="payload_diverged",
            non_retryable=True,
        )

    try:
        receipt = external_system.perform(payload, command.write_id)
    except TimeoutError as error:
        store.record_external_attempt(
            write_id=command.write_id,
            run_ref=command.run_ref,
            payload_hash=command.payload_hash,
            status="uncertain",
            receipt_ref=None,
        )
        store.event(
            command.run_ref,
            "review.created",
            {"subtype": "uncertain_write", "write_id": command.write_id},
        )
        raise ApplicationError(
            "uncertain external write requires human verification",
            type="uncertain_write",
            non_retryable=True,
        ) from error

    store.record_external_attempt(
        write_id=command.write_id,
        run_ref=command.run_ref,
        payload_hash=command.payload_hash,
        status="completed",
        receipt_ref=receipt,
    )
    return receipt
