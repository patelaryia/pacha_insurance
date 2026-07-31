"""Temporal Activities for the eight PRD-01 document-intelligence stages."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Any

from temporalio import activity

from doc_intel.engine import DocIntelEngine
from doc_intel.stages import TERMINAL_STAGE_STATUSES
from orchestration.contracts import ControlCommand, ControlHeartbeat, ControlResult
from orchestration.errors import sanitised_application_error

__all__ = [
    "DocumentIntelligenceActivities",
    "docintel_activity_registrations",
]

_BUILD_ID = re.compile(r"^[0-9a-f]{40}$")
_HEARTBEAT_SECONDS = 30


class DocumentIntelligenceActivities:
    """Bind Temporal's control-only edge to one ordinary ``DocIntelEngine``.

    The engine and its database/object/model dependencies stay inside the
    Activity process. Only the document ULID and integer stage checkpoint cross
    the Temporal boundary.
    """

    def __init__(self, engine: DocIntelEngine, *, worker_build_id: str) -> None:
        if not isinstance(engine, DocIntelEngine):
            raise RuntimeError("DocumentIntelligenceActivities requires a DocIntelEngine")
        if not isinstance(worker_build_id, str) or not _BUILD_ID.fullmatch(worker_build_id):
            raise RuntimeError(
                "DocumentIntelligenceActivities requires an immutable git SHA build id"
            )
        self._engine = engine
        self._worker_build_id = worker_build_id

    async def _execute(
        self,
        command: ControlCommand,
        *,
        stage: str,
        checkpoint: int,
    ) -> ControlResult:
        if command.document_ref is None or command.attempt_no != checkpoint:
            raise sanitised_application_error("domain_rejected")

        async def run_stage() -> dict[str, Any]:
            return await asyncio.to_thread(
                self._engine.process_stage,
                command.document_ref,
                stage,
                record_terminal_sample=True,
            )

        task = asyncio.create_task(run_stage())
        try:
            while not task.done():
                activity.heartbeat(
                    ControlHeartbeat(
                        step_id="ingest",
                        attempt_no=activity.info().attempt,
                        event_seq=checkpoint,
                    )
                )
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task),
                        timeout=_HEARTBEAT_SECONDS,
                    )
                except TimeoutError:
                    continue
            result = await task
        except asyncio.CancelledError:
            raise
        except Exception:
            raise sanitised_application_error("activity_internal") from None

        stage_status = result.get("status")
        if stage_status in TERMINAL_STAGE_STATUSES:
            status = "completed" if checkpoint == 8 else "running"
        else:
            # A failed, paused, still-running or otherwise non-terminal durable
            # stage never advances. An explicit recovery must first return the
            # database stage row to pending; an opaque Signal then wakes the
            # Workflow to observe that authorised state.
            status = "blocked"
        return ControlResult(
            status=status,
            run_ref=command.run_ref,
            attempt_no=checkpoint,
        )

    @activity.defn(name="docintel_normalize")
    async def normalize(self, command: ControlCommand) -> ControlResult:
        return await self._execute(command, stage="NORMALIZE", checkpoint=1)

    @activity.defn(name="docintel_classify")
    async def classify(self, command: ControlCommand) -> ControlResult:
        return await self._execute(command, stage="CLASSIFY", checkpoint=2)

    @activity.defn(name="docintel_split")
    async def split(self, command: ControlCommand) -> ControlResult:
        return await self._execute(command, stage="SPLIT", checkpoint=3)

    @activity.defn(name="docintel_extract")
    async def extract(self, command: ControlCommand) -> ControlResult:
        return await self._execute(command, stage="EXTRACT", checkpoint=4)

    @activity.defn(name="docintel_cite")
    async def cite(self, command: ControlCommand) -> ControlResult:
        return await self._execute(command, stage="CITE", checkpoint=5)

    @activity.defn(name="docintel_validate")
    async def validate(self, command: ControlCommand) -> ControlResult:
        return await self._execute(command, stage="VALIDATE", checkpoint=6)

    @activity.defn(name="docintel_commit")
    async def commit(self, command: ControlCommand) -> ControlResult:
        return await self._execute(command, stage="COMMIT", checkpoint=7)

    @activity.defn(name="docintel_consistency")
    async def consistency(self, command: ControlCommand) -> ControlResult:
        return await self._execute(command, stage="CONSISTENCY", checkpoint=8)


def docintel_activity_registrations(
    activities: DocumentIntelligenceActivities,
) -> tuple[Callable[..., Any], ...]:
    """The exact, ordered registration surface for the docintel Worker."""

    if not isinstance(activities, DocumentIntelligenceActivities):
        raise RuntimeError(
            "docintel_activity_registrations requires DocumentIntelligenceActivities"
        )
    return (
        activities.normalize,
        activities.classify,
        activities.split,
        activities.extract,
        activities.cite,
        activities.validate,
        activities.commit,
        activities.consistency,
    )
