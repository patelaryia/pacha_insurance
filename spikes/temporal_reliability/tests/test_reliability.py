from __future__ import annotations

import asyncio
from pathlib import Path

from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from temporal_spike.activities import SpikeActivities
from temporal_spike.codec import encrypted_data_converter
from temporal_spike.contracts import WorkflowInput, assert_control_only
from temporal_spike.gate import SyntheticExternalSystem
from temporal_spike.history_audit import assert_history_has_no_plaintext
from temporal_spike.store import AuthoritativeStore
from temporal_spike.workflows import DurableClaimWorkflow, DurableClaimWorkflowV1

KEY = b"0123456789abcdef0123456789abcdef"
QUEUE = "pacha-temporal-reliability"
CUSTOMER = "Synthetic Jane Doe"
BANK = "000111222333"


def _workflow_input(store: AuthoritativeStore, run_ref: str, timer_seconds: int = 0):
    claim_ref = f"claim:{run_ref}"
    payload_hash = store.seed_claim(
        claim_ref,
        customer_name=CUSTOMER,
        bank_account=BANK,
        target_payload={
            "claim_ref": claim_ref,
            "customer_name": CUSTOMER,
            "bank_account": BANK,
            "operation": "icon.claim_register",
        },
    )
    value = WorkflowInput(
        run_ref=run_ref,
        claim_ref=claim_ref,
        trigger_event_ref=f"event:{run_ref}",
        payload_hash=payload_hash,
        timer_seconds=timer_seconds,
    )
    assert_control_only(value)
    return value


async def _wait_for(predicate, timeout: float = 10) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise TimeoutError("condition not reached")
        await asyncio.sleep(0.05)


def _run_value(store: AuthoritativeStore, run_ref: str, key: str) -> str | None:
    try:
        return str(store.run(run_ref)[key])
    except KeyError:
        return None


async def _complete_review(store, handle, value) -> str:
    try:
        await _wait_for(
            lambda: _run_value(store, value.run_ref, "status") == "awaiting_review",
            timeout=10,
        )
    except TimeoutError as error:
        history = await handle.fetch_history()
        event_types = [event.WhichOneof("attributes") for event in history.events]
        raise AssertionError(
            f"review wait not reached; run={_run_value(store, value.run_ref, 'last_step')}; "
            f"history={event_types}"
        ) from error
    review_ref = f"review:{value.run_ref}"
    store.review(review_ref, "approved")
    await handle.signal(DurableClaimWorkflow.human_review, review_ref)
    return await handle.result()


def test_codec_round_trip_and_control_schema() -> None:
    async def scenario() -> None:
        converter = encrypted_data_converter(KEY)
        value = WorkflowInput("run:codec", "claim:codec", "event:codec", "a" * 64, 0)
        assert_control_only(value)
        payloads = await converter.encode([value])
        raw = b"".join(payload.SerializeToString() for payload in payloads)
        assert b"run:codec" not in raw
        assert b"binary/encrypted" in raw
        decoded = await converter.decode(payloads, [WorkflowInput])
        assert decoded == [value]

    asyncio.run(scenario())


def test_human_wait_duplicate_trigger_history_privacy_and_database_authority(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = AuthoritativeStore(tmp_path / "authority.db")
        value = _workflow_input(store, "run:happy")
        converter = encrypted_data_converter(KEY)
        async with await WorkflowEnvironment.start_time_skipping(
            data_converter=converter
        ) as env:
            activities = SpikeActivities(store)
            async with Worker(
                env.client,
                task_queue=QUEUE,
                workflows=[DurableClaimWorkflow],
                activities=activities.registered(),
                max_cached_workflows=0,
            ):
                handle = await env.client.start_workflow(
                    DurableClaimWorkflow.run,
                    value,
                    id=f"workflow:{value.run_ref}",
                    task_queue=QUEUE,
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                )
                duplicate = await env.client.start_workflow(
                    DurableClaimWorkflow.run,
                    value,
                    id=f"workflow:{value.run_ref}",
                    task_queue=QUEUE,
                    id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                )
                assert duplicate.first_execution_run_id == handle.first_execution_run_id
                assert await _complete_review(store, handle, value) == "completed"

                assert store.claim(value.claim_ref)["status"] == "COMPLETED"
                assert store.run(value.run_ref)["status"] == "completed"
                action = store.external_action(f"write:{value.run_ref}")
                assert action is not None
                assert action["attempts"] == 1
                assert action["status"] == "completed"

                history = await handle.fetch_history()
                assert_history_has_no_plaintext(history, (CUSTOMER, BANK))
        store.close()

    asyncio.run(scenario())


def test_worker_loss_uses_heartbeat_and_different_worker_resumes(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = AuthoritativeStore(tmp_path / "heartbeat.db")
        value = _workflow_input(store, "run:heartbeat")
        async with await WorkflowEnvironment.start_time_skipping(
            data_converter=encrypted_data_converter(KEY)
        ) as env:
            first = SpikeActivities(store, interrupt_after_checkpoint=1)
            worker_one = Worker(
                env.client,
                task_queue=QUEUE,
                workflows=[DurableClaimWorkflow],
                activities=first.registered(),
                max_cached_workflows=0,
            )
            worker_task = asyncio.create_task(worker_one.run())
            handle = await env.client.start_workflow(
                DurableClaimWorkflow.run,
                value,
                id=f"workflow:{value.run_ref}",
                task_queue=QUEUE,
            )
            await _wait_for(
                lambda: store.event_count(value.run_ref, "activity.interrupted") == 1
            )
            await worker_one.shutdown()
            await worker_task
            assert store.claim(value.claim_ref)["status"] == "INTIMATED"
            assert store.run(value.run_ref)["status"] == "running"

            second = SpikeActivities(store)
            async with Worker(
                env.client,
                task_queue=QUEUE,
                workflows=[DurableClaimWorkflow],
                activities=second.registered(),
                max_cached_workflows=0,
            ):
                assert await _complete_review(store, handle, value) == "completed"
            assert store.event_count(value.run_ref, "activity.heartbeat") >= 3
            assert store.external_action(f"write:{value.run_ref}")["attempts"] == 1
        store.close()

    asyncio.run(scenario())


def test_external_target_invocation_exists_only_in_gate() -> None:
    package = Path(__file__).parents[1] / "temporal_spike"
    invocations = [
        path.name
        for path in package.glob("*.py")
        if "external_system.perform(" in path.read_text()
    ]
    assert invocations == ["gate.py"]
    assert "execute_or_stage(" in (package / "activities.py").read_text()


def test_long_timer_survives_worker_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = AuthoritativeStore(tmp_path / "timer.db")
        value = _workflow_input(store, "run:timer", timer_seconds=60 * 60 * 24 * 30)
        async with await WorkflowEnvironment.start_time_skipping(
            data_converter=encrypted_data_converter(KEY)
        ) as env:
            activities = SpikeActivities(store)
            worker_one = Worker(
                env.client,
                task_queue=QUEUE,
                workflows=[DurableClaimWorkflow],
                activities=activities.registered(),
                max_cached_workflows=0,
            )
            worker_task = asyncio.create_task(worker_one.run())
            handle = await env.client.start_workflow(
                DurableClaimWorkflow.run,
                value,
                id=f"workflow:{value.run_ref}",
                task_queue=QUEUE,
            )
            await _wait_for(
                lambda: _run_value(store, value.run_ref, "last_step") == "validate"
            )
            await worker_one.shutdown()
            await worker_task

            async with Worker(
                env.client,
                task_queue=QUEUE,
                workflows=[DurableClaimWorkflow],
                activities=activities.registered(),
                max_cached_workflows=0,
            ):
                await env.sleep(60 * 60 * 24 * 31)
                assert await _complete_review(store, handle, value) == "completed"
        store.close()

    asyncio.run(scenario())


def test_uncertain_external_write_is_not_retried(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = AuthoritativeStore(tmp_path / "uncertain.db")
        value = _workflow_input(store, "run:uncertain")
        async with await WorkflowEnvironment.start_time_skipping(
            data_converter=encrypted_data_converter(KEY)
        ) as env:
            activities = SpikeActivities(
                store,
                external_system=SyntheticExternalSystem(mode="uncertain"),
            )
            async with Worker(
                env.client,
                task_queue=QUEUE,
                workflows=[DurableClaimWorkflow],
                activities=activities.registered(),
            ):
                handle = await env.client.start_workflow(
                    DurableClaimWorkflow.run,
                    value,
                    id=f"workflow:{value.run_ref}",
                    task_queue=QUEUE,
                )
                assert await _complete_review(store, handle, value) == "blocked"
            action = store.external_action(f"write:{value.run_ref}")
            assert action is not None
            assert action["status"] == "uncertain"
            assert action["attempts"] == 1
            assert store.run(value.run_ref)["status"] == "blocked"
            assert store.claim(value.claim_ref)["status"] == "INTIMATED"
        store.close()

    asyncio.run(scenario())


def test_newer_workflow_replays_old_history(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = AuthoritativeStore(tmp_path / "replay.db")
        value = _workflow_input(store, "run:replay")
        converter = encrypted_data_converter(KEY)
        async with await WorkflowEnvironment.start_time_skipping(
            data_converter=converter
        ) as env:
            activities = SpikeActivities(store)
            async with Worker(
                env.client,
                task_queue=QUEUE,
                workflows=[DurableClaimWorkflowV1],
                activities=activities.registered(),
            ):
                handle = await env.client.start_workflow(
                    DurableClaimWorkflowV1.run,
                    value,
                    id=f"workflow:{value.run_ref}",
                    task_queue=QUEUE,
                )
                await _wait_for(
                    lambda: _run_value(store, value.run_ref, "status")
                    == "awaiting_review"
                )
                review_ref = f"review:{value.run_ref}"
                store.review(review_ref, "approved")
                await handle.signal(DurableClaimWorkflowV1.human_review, review_ref)
                assert await handle.result() == "completed"
                history = await handle.fetch_history()

        result = await Replayer(
            workflows=[DurableClaimWorkflow],
            data_converter=converter,
        ).replay_workflow(history, raise_on_replay_failure=False)
        assert result.replay_failure is None
        store.close()

    asyncio.run(scenario())
