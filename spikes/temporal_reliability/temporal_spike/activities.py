"""Activities that load facts inside Pacha and return control-only results."""

from __future__ import annotations

import asyncio
import hashlib

from temporalio import activity

from .contracts import (
    ControlResult,
    ExternalActionCommand,
    HeartbeatCommand,
    ReviewCommand,
    RunRef,
    StepCommand,
)
from .gate import SyntheticExternalSystem, execute_or_stage
from .store import AuthoritativeStore


class SpikeActivities:
    def __init__(
        self,
        store: AuthoritativeStore,
        *,
        external_system: SyntheticExternalSystem | None = None,
        interrupt_after_checkpoint: int | None = None,
    ) -> None:
        self.store = store
        self.external_system = external_system or SyntheticExternalSystem()
        self.interrupt_after_checkpoint = interrupt_after_checkpoint

    @activity.defn(name="record_started")
    async def record_started(self, command: RunRef) -> ControlResult:
        self.store.upsert_run(command.run_ref, command.claim_ref, "running", "started")
        self.store.event(command.run_ref, "workflow.started", {"claim_ref": command.claim_ref})
        return ControlResult("running", "started", "")

    @activity.defn(name="automated_step")
    async def automated_step(self, command: StepCommand) -> ControlResult:
        claim = self.store.claim(command.claim_ref)
        digest = hashlib.sha256(
            f"{command.step}:{claim['target_payload']}".encode()
        ).hexdigest()
        self.store.upsert_run(command.run_ref, command.claim_ref, "running", command.step)
        self.store.event(
            command.run_ref,
            "step.completed",
            {"step": command.step, "output_hash": digest},
        )
        return ControlResult("completed", command.step, digest)

    @activity.defn(name="heartbeating_step")
    async def heartbeating_step(self, command: HeartbeatCommand) -> ControlResult:
        details = activity.info().heartbeat_details
        start = int(details[0]) if details else 0
        for checkpoint in range(start + 1, command.checkpoint_count + 1):
            self.store.upsert_run(
                command.run_ref,
                command.claim_ref,
                "running",
                f"heartbeat:{checkpoint}",
            )
            self.store.event(
                command.run_ref,
                "activity.heartbeat",
                {"checkpoint": checkpoint},
            )
            activity.heartbeat(checkpoint)
            if self.interrupt_after_checkpoint == checkpoint:
                self.store.event(
                    command.run_ref,
                    "activity.interrupted",
                    {"checkpoint": checkpoint},
                )
                raise RuntimeError("injected worker interruption after heartbeat")
            await asyncio.sleep(0.05)
        digest = hashlib.sha256(
            f"{command.run_ref}:{command.checkpoint_count}".encode()
        ).hexdigest()
        return ControlResult("completed", "heartbeating", digest)

    @activity.defn(name="record_awaiting_review")
    async def record_awaiting_review(self, command: RunRef) -> ControlResult:
        self.store.upsert_run(
            command.run_ref,
            command.claim_ref,
            "awaiting_review",
            "human_review",
        )
        self.store.event(command.run_ref, "review.created", {"status": "awaiting_review"})
        return ControlResult("awaiting_review", "human_review", "")

    @activity.defn(name="apply_review")
    async def apply_review(self, command: ReviewCommand) -> ControlResult:
        decision = self.store.review_decision(command.review_event_ref)
        if decision != "approved":
            raise ValueError("synthetic review was not approved")
        digest = hashlib.sha256(command.review_event_ref.encode()).hexdigest()
        self.store.event(
            command.run_ref,
            "review.resolved",
            {"review_event_ref": command.review_event_ref},
        )
        return ControlResult("approved", "human_review", digest)

    @activity.defn(name="governed_external_action")
    async def governed_external_action(
        self,
        command: ExternalActionCommand,
    ) -> ControlResult:
        receipt = execute_or_stage(
            command,
            store=self.store,
            external_system=self.external_system,
        )
        receipt_hash = hashlib.sha256(receipt.encode()).hexdigest()
        self.store.event(
            command.run_ref,
            "external.completed",
            {"write_id": command.write_id, "receipt_hash": receipt_hash},
        )
        return ControlResult("completed", "external_action", receipt_hash)

    @activity.defn(name="record_compatibility_marker")
    async def record_compatibility_marker(self, command: RunRef) -> ControlResult:
        self.store.event(command.run_ref, "workflow.compatibility", {"version": "v2"})
        return ControlResult("completed", "compatibility", "")

    @activity.defn(name="record_completed")
    async def record_completed(self, command: RunRef) -> ControlResult:
        self.store.complete_claim(command.claim_ref)
        self.store.upsert_run(
            command.run_ref,
            command.claim_ref,
            "completed",
            "completed",
        )
        self.store.event(command.run_ref, "workflow.completed", {"status": "completed"})
        return ControlResult("completed", "completed", "")

    @activity.defn(name="record_blocked")
    async def record_blocked(self, command: RunRef) -> ControlResult:
        self.store.upsert_run(
            command.run_ref,
            command.claim_ref,
            "blocked",
            "uncertain_write",
        )
        return ControlResult("blocked", "uncertain_write", "")

    def registered(self) -> list[object]:
        return [
            self.record_started,
            self.automated_step,
            self.heartbeating_step,
            self.record_awaiting_review,
            self.apply_review,
            self.governed_external_action,
            self.record_compatibility_marker,
            self.record_completed,
            self.record_blocked,
        ]
