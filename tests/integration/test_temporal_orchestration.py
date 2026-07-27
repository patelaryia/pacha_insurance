"""T01 integration suite — the real Temporal SDK, not a mocked engine.

Master plan section 22 forbids mocking Workflow execution for acceptance
behaviour, so everything here runs against `WorkflowEnvironment.start_time_skipping`:
a real server, a real Worker, real Workflow tasks, a real 30-day durable timer.

What these tests are for:

* prove the encrypted Data Converter is applied to every payload class Temporal
  records — Workflow input and result, Signal, Activity input and output,
  heartbeat detail and failure;
* prove the AR-1 minimisation rule directly, by seeding a name, a policy number,
  a registration plate, bank data, a national ID, a money figure, document text,
  a credential, a narrative and an email address into the Activity's authorised
  store and then finding none of them in the history Temporal recorded;
* prove the Worker factory really applies each role's Task Queue, concurrency,
  build ID and pinned versioning.

The Workflows and Activities are the test-only ones in `support.temporal`; T01
ships no production Workflow.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest
from temporalio.api.common.v1 import Payload
from temporalio.client import (
    Client,
    Interceptor,
    OutboundInterceptor,
    StartWorkflowInput,
    WorkflowFailureError,
    WorkflowQueryFailedError,
)
from temporalio.common import (
    VersioningBehavior,
    WorkflowIDConflictPolicy,
    WorkflowIDReusePolicy,
)
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.testing import WorkflowEnvironment

from orchestration.client import build_temporal_client
from orchestration.codec import CODEC_ENCODING, PachaPayloadCodec, StaticDataKeyProvider
from orchestration.config import WORKER_ROLES
from orchestration.contracts import (
    ControlCommand,
    ControlPayloadConverter,
    ControlPayloadInterceptor,
    ControlSignal,
)
from orchestration.errors import ControlContractError
from orchestration.history import (
    assert_no_sentinels,
    assert_workflow_history_private,
    decoded_history_blob,
    find_sentinels,
    history_blob,
)
from orchestration.ids import (
    agent_workflow_ref,
    chase_workflow_ref,
    docintel_workflow_ref,
    projection_workflow_ref,
)
from orchestration.worker import ROLE_ACTIVITY_CONCURRENCY, build_worker
from support.temporal import (
    CHECKLIST_REF,
    CLAIM_REF,
    DOCUMENT_REF,
    EVENT_REF,
    PRIVACY_SENTINELS,
    PROJECTION_REF,
    RUN_REF,
    STATIC_CODEC_KEY,
    ControlProbeWorkflow,
    FailingProbeWorkflow,
    HeartbeatProbeWorkflow,
    control_activity,
    failing_activity,
    heartbeat_probe_activity,
    local_config,
    plain_client_for,
    static_data_converter,
)

_SENTINELS = tuple(PRIVACY_SENTINELS.values())


class _HeaderInjectingOutbound(OutboundInterceptor):
    async def start_workflow(self, input: StartWorkflowInput):  # type: ignore[override]
        input.headers["x-pacha-test-pii"] = Payload(
            metadata={"encoding": b"json/plain"},
            data=PRIVACY_SENTINELS["insured_name"].encode(),
        )
        return await super().start_workflow(input)


class _HeaderInjectingInterceptor(Interceptor):
    """A hostile downstream interceptor used to prove Pacha validates last."""

    def intercept_client(self, next: OutboundInterceptor) -> OutboundInterceptor:
        return _HeaderInjectingOutbound(next)


class _Harness:
    """One time-skipping server, one running control Worker, one event loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop, env: Any, config: Any) -> None:
        self.loop = loop
        self.env = env
        self.config = config

    @property
    def client(self) -> Client:
        return self.env.client

    def run(self, coro: Any) -> Any:
        return self.loop.run_until_complete(coro)

    @property
    def codec(self) -> Any:
        return self.client.data_converter.payload_codec

    def decoded_history(self, handle: Any) -> bytes:
        """The history as Pacha wrote it, with every Pacha payload decrypted."""

        async def _fetch() -> bytes:
            return await decoded_history_blob(await handle.fetch_history(), self.codec)

        return self.run(_fetch())

    def stored_history(self, workflow_id: str) -> bytes:
        """The history exactly as Temporal stores it, read without a Codec."""

        async def _fetch() -> bytes:
            plain = await plain_client_for(self.client)
            return history_blob(await plain.get_workflow_handle(workflow_id).fetch_history())

        return self.run(_fetch())

    def build_worker(self, role: str, *, config: Any = None, **kwargs: Any) -> Any:
        """Build a Worker on the harness loop.

        Worker construction prepares the sandbox, which needs a running event
        loop, so it cannot happen between `run_until_complete` calls.
        """

        async def _build() -> Any:
            return build_worker(self.client, config or self.config, role=role, **kwargs)

        return self.run(_build())

    def worker_settings(self, role: str, *, config: Any = None, **kwargs: Any) -> tuple[str, Any]:
        """Build a Worker, read its applied configuration, then shut it down.

        Constructing a Worker creates its bridge worker and starts pollers, so a
        probe that is never entered leaves them polling a server that is about to
        disappear. Running it through its context manager keeps the suite's
        teardown quiet and its shutdown path exercised.
        """

        async def _probe() -> tuple[str, Any]:
            worker = build_worker(self.client, config or self.config, role=role, **kwargs)
            settings = worker.config()
            task_queue = worker.task_queue
            async with worker:
                pass
            return task_queue, settings

        return self.run(_probe())

    def start(self, workflow: Any, command: ControlCommand, workflow_id: str) -> Any:
        return self.run(
            self.client.start_workflow(
                workflow,
                command,
                id=workflow_id,
                task_queue=self.config.task_queue("control"),
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            )
        )


@pytest.fixture(scope="module")
def harness():
    config = local_config()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    # No skip guard. A test environment that will not start, a Worker that will
    # not register and an SDK misconfiguration are defects, and turning them
    # into skips reports green for a broken substrate (master plan §22).
    env = loop.run_until_complete(
        WorkflowEnvironment.start_time_skipping(
            data_converter=static_data_converter(config),
            interceptors=[ControlPayloadInterceptor()],
        )
    )

    running = _Harness(loop, env, config)
    worker = running.build_worker(
        "control",
        workflows=[ControlProbeWorkflow, FailingProbeWorkflow, HeartbeatProbeWorkflow],
        activities=[control_activity, failing_activity, heartbeat_probe_activity],
    )
    loop.run_until_complete(worker.__aenter__())
    try:
        yield running
    finally:
        loop.run_until_complete(worker.__aexit__(None, None, None))
        loop.run_until_complete(env.__aexit__(None, None, None))
        asyncio.set_event_loop(None)
        loop.close()


@pytest.fixture(scope="module")
def completed_probe(harness):
    """Run the control probe once: Activity, Signal, 30-day timer, result."""

    command = ControlCommand(
        run_ref=RUN_REF,
        claim_ref=CLAIM_REF,
        checklist_ref=CHECKLIST_REF,
        step_id="ingest",
    )
    handle = harness.start(
        ControlProbeWorkflow.run, command, str(chase_workflow_ref(CHECKLIST_REF))
    )
    harness.run(handle.signal(ControlProbeWorkflow.pacha_event, ControlSignal(event_ref=EVENT_REF)))
    result = harness.run(handle.result())
    return handle, result


def test_the_probe_completes_through_an_activity_a_signal_and_a_durable_timer(completed_probe):
    _, result = completed_probe
    assert result.status == "completed"
    assert result.run_ref == RUN_REF
    assert result.event_ref == EVENT_REF
    assert len(result.payload_hash) == 64


def test_a_signal_is_delivered_as_one_opaque_event_reference(harness, completed_probe):
    handle, _ = completed_probe
    result = harness.run(handle.query(ControlProbeWorkflow.observed_event_count))
    assert result.status == "running"
    assert result.event_seq == 1


def test_fetched_history_carries_no_seeded_privacy_sentinel(harness, completed_probe):
    """Section 22.5 — the decoded history is the strong form of the AR-1 rule.

    Decoding first means a clean scan proves the sentinel was never in the
    payload at all, rather than merely that encryption hid it.
    """

    handle, _ = completed_probe
    decoded = harness.decoded_history(handle)
    assert find_sentinels(decoded, _SENTINELS) == []
    assert_no_sentinels(decoded, _SENTINELS, source="decoded workflow history")
    harness.run(assert_workflow_history_private(handle, _SENTINELS, codec=harness.codec))
    # The control values genuinely travelled: they are present once decoded, so
    # the clean scan is not an artefact of an empty or unreadable history.
    assert RUN_REF.encode() in decoded
    assert EVENT_REF.encode() in decoded


def test_the_stored_history_is_ciphertext_under_the_pacha_codec(harness, completed_probe):
    handle, _ = completed_probe
    raw = harness.stored_history(handle.id)
    assert CODEC_ENCODING in raw
    assert find_sentinels(raw, _SENTINELS) == []
    # Control references live only inside payloads, so encryption must hide them.
    assert RUN_REF.encode() not in raw
    assert EVENT_REF.encode() not in raw


def test_an_activity_heartbeat_survives_a_retry_and_stays_control_only(harness):
    command = ControlCommand(run_ref=RUN_REF, claim_ref=CLAIM_REF, document_ref=DOCUMENT_REF)
    handle = harness.start(
        HeartbeatProbeWorkflow.run, command, str(docintel_workflow_ref(DOCUMENT_REF))
    )
    result = harness.run(handle.result())
    assert result.attempt_no == 2  # resumed on a second attempt
    assert result.step_id == "ingest"  # recovered from the heartbeat detail

    assert find_sentinels(harness.decoded_history(handle), _SENTINELS) == []
    raw = harness.stored_history(handle.id)
    assert find_sentinels(raw, _SENTINELS) == []
    assert CODEC_ENCODING in raw


def test_an_activity_failure_reaches_history_as_a_sanitised_classification(harness):
    command = ControlCommand(run_ref=RUN_REF, claim_ref=CLAIM_REF)
    handle = harness.start(FailingProbeWorkflow.run, command, str(agent_workflow_ref(RUN_REF)))

    with pytest.raises(WorkflowFailureError) as caught:
        harness.run(handle.result())

    activity_error = caught.value.cause
    assert isinstance(activity_error, ActivityError)
    application_error = activity_error.cause
    assert isinstance(application_error, ApplicationError)
    assert application_error.type == "blocked_on_inputs"
    assert application_error.message == "blocked_on_inputs"
    assert application_error.non_retryable is True

    # The Activity's internal RuntimeError named the policy number and the
    # insured. Neither the raised failure nor the recorded history may repeat it.
    assert find_sentinels(str(caught.value), _SENTINELS) == []
    assert find_sentinels(harness.decoded_history(handle), _SENTINELS) == []
    assert find_sentinels(harness.stored_history(handle.id), _SENTINELS) == []


def test_a_governed_write_activity_is_attempted_exactly_once(harness, completed_probe):
    """`governed_external_write` caps Temporal attempts at one (section 12)."""

    handle = harness.client.get_workflow_handle(str(agent_workflow_ref(RUN_REF)))
    events = harness.run(handle.fetch_history()).events
    started = [
        event
        for event in events
        if event.HasField("activity_task_started_event_attributes")
    ]
    assert len(started) == 1


def test_a_duplicate_start_attaches_to_the_existing_execution(harness):
    command = ControlCommand(run_ref=RUN_REF, claim_ref=CLAIM_REF, projection_ref=PROJECTION_REF)
    workflow_id = str(projection_workflow_ref(PROJECTION_REF))
    first = harness.start(ControlProbeWorkflow.run, command, workflow_id)
    second = harness.start(ControlProbeWorkflow.run, command, workflow_id)
    assert first.result_run_id == second.result_run_id

    harness.run(second.signal(ControlProbeWorkflow.pacha_event, ControlSignal(event_ref=EVENT_REF)))
    assert harness.run(first.result()).status == "completed"


@pytest.mark.parametrize("role", WORKER_ROLES)
def test_each_worker_role_applies_its_queue_concurrency_build_and_versioning(harness, role):
    # A distinct build ID, so probing the `control` role does not collide with
    # the Worker the harness already has polling that queue.
    config = replace(harness.config, build_id="c" * 40)
    task_queue, settings = harness.worker_settings(
        role, config=config, activities=[control_activity]
    )

    assert task_queue == f"pacha-test-{role}-v1"
    assert settings["max_concurrent_activities"] == ROLE_ACTIVITY_CONCURRENCY[role]
    assert settings["graceful_shutdown_timeout"].total_seconds() == 60

    deployment = settings["deployment_config"]
    assert deployment.version.deployment_name == f"pacha-test-{role}"
    assert deployment.version.build_id == config.build_id
    assert deployment.use_worker_versioning is True
    assert deployment.default_versioning_behavior is VersioningBehavior.PINNED


def test_build_temporal_client_connects_with_the_codec_and_the_interceptor(harness):
    """The factory, not the call site, decides the security configuration."""

    config = replace(harness.config, address=harness.client.service_client.config.target_host)
    client = harness.run(
        build_temporal_client(
            config,
            data_key_provider=StaticDataKeyProvider(STATIC_CODEC_KEY),
        )
    )
    assert isinstance(client.data_converter.payload_codec, PachaPayloadCodec)

    assert isinstance(client.data_converter.payload_converter, ControlPayloadConverter)

    # The mandatory interceptor is live on the connection the factory returned.
    with pytest.raises(ControlContractError, match="not an allowlisted control field"):
        harness.run(
            client.start_workflow(
                "PachaControlProbeWorkflow",
                {"insured_name": PRIVACY_SENTINELS["insured_name"]},
                id=str(agent_workflow_ref(DOCUMENT_REF)),
                task_queue=config.task_queue("control"),
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            )
        )


def test_the_mandatory_interceptor_rejects_headers_added_by_an_earlier_interceptor(harness):
    config = replace(harness.config, address=harness.client.service_client.config.target_host)
    client = harness.run(
        build_temporal_client(
            config,
            data_key_provider=StaticDataKeyProvider(STATIC_CODEC_KEY),
            interceptors=[_HeaderInjectingInterceptor()],
        )
    )

    with pytest.raises(ControlContractError, match="custom headers"):
        harness.run(
            client.start_workflow(
                ControlProbeWorkflow.run,
                ControlCommand(run_ref=RUN_REF),
                id=str(agent_workflow_ref(DOCUMENT_REF)),
                task_queue=config.task_queue("control"),
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            )
        )


def test_the_converter_refuses_a_query_result_no_interceptor_could_see(harness, completed_probe):
    """Regression: only start/signal/query *arguments* were validated.

    A Query result never passes the client interceptor, so before the validating
    converter existed a Workflow could hand back an arbitrary dictionary and the
    Codec would faithfully encrypt claim facts into history.
    """

    handle, _ = completed_probe
    with pytest.raises(WorkflowQueryFailedError) as caught:
        harness.run(handle.query(ControlProbeWorkflow.leaky_snapshot))

    # Refused at serialization, and the refusal itself repeats no claim fact.
    assert find_sentinels(str(caught.value), _SENTINELS) == []
    assert find_sentinels(harness.decoded_history(handle), _SENTINELS) == []
    assert find_sentinels(harness.stored_history(handle.id), _SENTINELS) == []


def test_a_start_must_pin_the_declared_reuse_and_conflict_policies(harness):
    command = ControlCommand(run_ref=RUN_REF, claim_ref=CLAIM_REF)
    with pytest.raises(ControlContractError, match="REJECT_DUPLICATE"):
        harness.run(
            harness.client.start_workflow(
                ControlProbeWorkflow.run,
                command,
                id=str(chase_workflow_ref(CLAIM_REF)),
                task_queue=harness.config.task_queue("control"),
            )
        )
    with pytest.raises(ControlContractError, match="USE_EXISTING"):
        harness.run(
            harness.client.start_workflow(
                ControlProbeWorkflow.run,
                command,
                id=str(chase_workflow_ref(CLAIM_REF)),
                task_queue=harness.config.task_queue("control"),
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        )


def test_unapproved_sdk_surfaces_are_refused(harness, completed_probe):
    handle, _ = completed_probe
    with pytest.raises(ControlContractError, match="Workflow Updates"):
        harness.run(handle.execute_update("anything"))


def test_the_client_interceptor_refuses_forbidden_data_before_the_sdk_call(harness):
    with pytest.raises(ControlContractError, match="not an allowlisted control field"):
        harness.run(
            harness.client.start_workflow(
                "PachaControlProbeWorkflow",
                {"insured_name": PRIVACY_SENTINELS["insured_name"]},
                id=str(chase_workflow_ref(CLAIM_REF)),
                task_queue=harness.config.task_queue("control"),
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            )
        )


def test_the_client_interceptor_refuses_an_undeclared_workflow_id(harness):
    with pytest.raises(ControlContractError, match="workflow_ref"):
        harness.run(
            harness.client.start_workflow(
                "PachaControlProbeWorkflow",
                ControlCommand(run_ref=RUN_REF),
                id=f"pacha.reaper.{RUN_REF}",
                task_queue=harness.config.task_queue("control"),
            )
        )


def test_the_client_interceptor_refuses_memo_and_search_attributes(harness):
    with pytest.raises(ControlContractError, match="memo"):
        harness.run(
            harness.client.start_workflow(
                "PachaControlProbeWorkflow",
                ControlCommand(run_ref=RUN_REF),
                id=str(agent_workflow_ref(CLAIM_REF)),
                task_queue=harness.config.task_queue("control"),
                memo={"insured": PRIVACY_SENTINELS["insured_name"]},
            )
        )


def test_the_client_interceptor_refuses_a_non_control_signal(harness, completed_probe):
    handle, _ = completed_probe
    with pytest.raises(ControlContractError):
        harness.run(handle.signal("pacha_event", {"resolution": "approved by the branch manager"}))
