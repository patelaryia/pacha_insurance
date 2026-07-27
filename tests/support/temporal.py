"""Shared builders and test-only Workflows/Activities for the Temporal substrate.

The master plan keeps production Workflows out of T01, so the Workflow and
Activities exercised by the T01 suites live here rather than in
`platform/orchestration`. They are deliberately shaped like the real thing:
control-only inputs and results, PII loaded *inside* an Activity from a store
the Workflow cannot see, a sanitised failure classification, a heartbeat that
survives a retry, a durable timer and an opaque Signal.

`SENSITIVE_CLAIM_STORE` stands in for Pacha's authorised stores. Everything in
`PRIVACY_SENTINELS` is seeded through the code path under test and must never
appear in fetched history — that assertion is the point of the whole module.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import VersioningBehavior
from temporalio.converter import DataConverter

with workflow.unsafe.imports_passed_through():
    from orchestration.codec import StaticDataKeyProvider, build_data_converter
    from orchestration.config import TemporalConfig
    from orchestration.contracts import (
        ControlCommand,
        ControlHeartbeat,
        ControlResult,
        ControlSignal,
    )
    from orchestration.errors import sanitised_application_error
    from orchestration.policies import load_retry_policies

__all__ = [
    "APPLIED_REVIEW_EVENTS",
    "CHECKLIST_REF",
    "CLAIM_REF",
    "DOCUMENT_REF",
    "EVENT_REF",
    "PRIVACY_SENTINELS",
    "PROJECTION_REF",
    "RUN_REF",
    "SENSITIVE_CLAIM_STORE",
    "STATIC_CODEC_KEY",
    "TRIGGER_EVENT_REF",
    "ControlProbeWorkflow",
    "FailingProbeWorkflow",
    "HeartbeatProbeWorkflow",
    "FakeKmsClient",
    "FakeSecretsManagerClient",
    "FakeSecretProvider",
    "FailingKmsClient",
    "ReviewWaitWorkflow",
    "apply_review_activity",
    "cloud_config",
    "cloud_environ",
    "control_activity",
    "failing_activity",
    "heartbeat_probe_activity",
    "local_config",
    "local_environ",
    "plain_client_for",
    "review_wait_mapping",
    "static_data_converter",
    "CLOUD_KMS_KEY_ARN",
]

#: The immutable KMS key ARN used by the cloud fixtures. Alias ARNs are refused
#: by configuration, so no fixture may use one (master plan §11).
CLOUD_KMS_KEY_ARN = (
    "arn:aws:kms:af-south-1:123456789012:key/3f2504e0-4f89-41d3-9a0c-0305e82c3301"
)

# --- opaque references -------------------------------------------------------

RUN_REF = "01JZ8Q9R7K4M2N6P8T0V3W5Y7Z"
CLAIM_REF = "01JZ8QA0B1C2D3E4F5G6H7J8K9"
CHECKLIST_REF = "01JZ8QB2M3N4P5Q6R7S8T9V0W1"
DOCUMENT_REF = "01JZ8QC3X4Y5Z6A7B8C9D0E1F2"
PROJECTION_REF = "01JZ8QD4G5H6J7K8M9N0P1Q2R3"
TRIGGER_EVENT_REF = "01JZ8QE5S6T7V8W9X0Y1Z2A3B4"
EVENT_REF = "01JZ8QF6C7D8E9F0G1H2J3K4M5"
REVIEW_EVENT_REF = "01JZ8QG7N8P9Q0R1S2T3V4W5X6"
WORKFLOW_RUN_REF = "6f8b2c1d-4e5a-4b7c-9d0e-1f2a3b4c5d6e"

# --- privacy sentinels -------------------------------------------------------

#: Seeded through every code path under test; none may reach Workflow history.
PRIVACY_SENTINELS: dict[str, str] = {
    "insured_name": "Wanjiru Kamau",
    "policy_number": "MOT/2026/0099471",
    "registration": "KDA 812X",
    "bank_account": "01102938475600",
    "national_id": "A012345678Z",
    "money": "KES 1,482,500.00",
    "document_text": "Assessor report page 3 records a bent chassis rail",
    "credential": "Bearer sk-live-9f3c2a7e",
    "narrative": "The claimant states the matatu overtook on the inside",
    "recipient": "wanjiru.kamau@example.co.ke",
}

#: Stands in for Pacha's authorised stores. Activities read it; nothing returns it.
SENSITIVE_CLAIM_STORE: dict[str, dict[str, str]] = {CLAIM_REF: dict(PRIVACY_SENTINELS)}

# --- Codec and configuration seams -------------------------------------------

STATIC_CODEC_KEY = bytes(range(32))


def local_environ(**overrides: str) -> dict[str, str]:
    """A complete, valid `dev`/local environment mapping."""

    environ = {
        "PACHA_ENV": "test",
        "PACHA_TEMPORAL_MODE": "local",
        "PACHA_TEMPORAL_NAMESPACE": "default",
        "PACHA_TEMPORAL_QUEUE_PREFIX": "pacha-test",
        "PACHA_BUILD_ID": "a" * 40,
    }
    environ.update(overrides)
    return environ


def cloud_environ(**overrides: str) -> dict[str, str]:
    """A complete, valid `staging`/cloud environment mapping."""

    environ = {
        "PACHA_ENV": "staging",
        "PACHA_TEMPORAL_MODE": "cloud",
        "PACHA_TEMPORAL_ADDRESS": "pacha-staging.a1b2c.tmprl.cloud:7233",
        "PACHA_TEMPORAL_NAMESPACE": "pacha-staging.a1b2c",
        "PACHA_TEMPORAL_REGION": "af-south-1",
        "PACHA_TEMPORAL_TLS_CERT_SECRET_ARN": (
            "arn:aws:secretsmanager:af-south-1:123456789012:secret:pacha/temporal/cert-AbCdEf"
        ),
        "PACHA_TEMPORAL_TLS_KEY_SECRET_ARN": (
            "arn:aws:secretsmanager:af-south-1:123456789012:secret:pacha/temporal/key-AbCdEf"
        ),
        "PACHA_TEMPORAL_KMS_KEY_ARN": CLOUD_KMS_KEY_ARN,
        "PACHA_TEMPORAL_QUEUE_PREFIX": "pacha-staging",
        "PACHA_BUILD_ID": "b" * 40,
    }
    environ.update(overrides)
    return environ


def local_config(**overrides: str) -> TemporalConfig:
    return TemporalConfig.from_environ(local_environ(**overrides))


def cloud_config(**overrides: str) -> TemporalConfig:
    return TemporalConfig.from_environ(cloud_environ(**overrides))


def static_data_converter(config: TemporalConfig | None = None) -> DataConverter:
    """The encrypted Data Converter backed by the synthetic test key."""

    return build_data_converter(
        config or local_config(),
        data_key_provider=StaticDataKeyProvider(STATIC_CODEC_KEY),
    )


@dataclass
class FakeSecretProvider:
    """A `SecretBytesProvider` that records which ARNs were requested."""

    secrets: dict[str, bytes]
    requested: list[str] = field(default_factory=list)

    def secret_bytes(self, secret_arn: str) -> bytes:
        self.requested.append(secret_arn)
        try:
            return self.secrets[secret_arn]
        except KeyError:  # pragma: no cover - a test asking for the wrong ARN
            raise AssertionError("unexpected secret ARN requested") from None


@dataclass
class FakeSecretsManagerClient:
    """The boto3 Secrets Manager surface `AwsSecretsManagerProvider` uses."""

    values: dict[str, str]

    def get_secret_value(self, *, SecretId: str) -> dict[str, str]:  # noqa: N803 - boto3 API
        return {"SecretString": self.values[SecretId]}


@dataclass
class FakeKmsClient:
    """The boto3 KMS surface `KmsDataKeyProvider` uses, with a fixed data key.

    Faithful on the one behaviour that matters here: `GenerateDataKey` echoes
    the **canonical key ARN** in `KeyId` however the caller addressed the key.
    Addressing it by alias therefore yields a response the caller's own
    allowlist will not recognise — the failure the key-ARN-only rule prevents.
    """

    key_arn: str
    data_key: bytes = STATIC_CODEC_KEY
    generate_calls: int = 0
    decrypt_calls: int = 0
    requested_key_ids: list[str] = field(default_factory=list)

    def generate_data_key(self, *, KeyId: str, KeySpec: str) -> dict[str, Any]:  # noqa: N803
        assert KeySpec == "AES_256"
        self.generate_calls += 1
        self.requested_key_ids.append(KeyId)
        return {
            "KeyId": self.key_arn,
            "Plaintext": self.data_key,
            "CiphertextBlob": b"wrapped:" + self.data_key,
        }

    def decrypt(self, *, KeyId: str, CiphertextBlob: bytes) -> dict[str, Any]:  # noqa: N803
        self.decrypt_calls += 1
        if KeyId != self.key_arn or CiphertextBlob != b"wrapped:" + self.data_key:
            raise RuntimeError("unknown wrapped data key")
        return {"Plaintext": self.data_key}


class FailingKmsClient:
    """A KMS client that refuses every call, standing in for an outage."""

    def generate_data_key(self, **_: Any) -> dict[str, Any]:
        raise RuntimeError("kms unavailable")

    def decrypt(self, **_: Any) -> dict[str, Any]:
        raise RuntimeError("kms unavailable")


async def plain_client_for(client: Client) -> Client:
    """A second client on the same server with no Codec, for raw history reads."""

    return await Client.connect(
        client.service_client.config.target_host,
        namespace=client.namespace,
    )


# --- test-only Activities ----------------------------------------------------


@activity.defn(name="pacha_test_control")
async def control_activity(command: ControlCommand) -> ControlResult:
    """Load claim data inside the Activity and return control values only."""

    record = SENSITIVE_CLAIM_STORE[command.claim_ref or ""]
    digest = hashlib.sha256(
        "|".join(f"{key}={value}" for key, value in sorted(record.items())).encode("utf-8")
    ).hexdigest()
    activity.heartbeat(ControlHeartbeat(step_id="ingest", attempt_no=activity.info().attempt))
    return ControlResult(
        status="running",
        run_ref=command.run_ref,
        claim_ref=command.claim_ref,
        step_id="ingest",
        payload_hash=digest,
        attempt_no=activity.info().attempt,
    )


@activity.defn(name="pacha_test_failing")
async def failing_activity(command: ControlCommand) -> ControlResult:
    """Raise a sanitised classification after an internal PII-bearing failure."""

    record = SENSITIVE_CLAIM_STORE[command.claim_ref or ""]
    caught: Exception | None = None
    try:
        raise RuntimeError(
            f"upstream rejected {record['policy_number']} for {record['insured_name']}"
        )
    except RuntimeError as error:
        caught = error
    assert caught is not None
    # The raw text is what Pacha/Sentry would receive; Temporal gets the
    # classification only, and never the exception chain that carried the value.
    raise sanitised_application_error(
        "blocked_on_inputs",
        details={"run_ref": command.run_ref, "step_id": "populate"},
    )


@activity.defn(name="pacha_test_heartbeat")
async def heartbeat_probe_activity(command: ControlCommand) -> ControlResult:
    """Heartbeat, fail once, then resume from the recorded heartbeat detail."""

    info = activity.info()
    if info.attempt == 1:
        activity.heartbeat(ControlHeartbeat(step_id="ingest", attempt_no=info.attempt))
        raise sanitised_application_error("activity_internal", details={"run_ref": command.run_ref})
    recorded = info.heartbeat_details[0] if info.heartbeat_details else {}
    step_id = recorded.get("step_id", "ingest") if isinstance(recorded, dict) else "ingest"
    return ControlResult(
        status="running",
        run_ref=command.run_ref,
        step_id=step_id,
        attempt_no=info.attempt,
    )


# --- test-only Workflow ------------------------------------------------------


@workflow.defn(name="PachaControlProbeWorkflow", versioning_behavior=VersioningBehavior.PINNED)
class ControlProbeWorkflow:
    """An Activity call, an opaque Signal wait and a durable timer.

    Deliberately finite and pinned, exactly as the master plan requires of every
    Pacha Workflow.
    """

    def __init__(self) -> None:
        self._event_refs: list[str] = []

    @workflow.run
    async def run(self, command: ControlCommand) -> ControlResult:
        policy = load_retry_policies()["db_control"]
        started = await workflow.execute_activity(
            control_activity,
            command,
            start_to_close_timeout=policy.start_to_close_timeout,
            retry_policy=policy.retry_policy,
        )
        await workflow.wait_condition(lambda: bool(self._event_refs))
        await asyncio.sleep(timedelta(days=30).total_seconds())
        return ControlResult(
            status="completed",
            run_ref=command.run_ref,
            claim_ref=command.claim_ref,
            event_ref=self._event_refs[0],
            payload_hash=started.payload_hash,
            step_id="ingest",
        )

    @workflow.signal(name="pacha_event")
    async def pacha_event(self, signal: ControlSignal) -> None:
        """Enqueue the opaque reference; no database call, no decision."""

        if signal.event_ref not in self._event_refs:
            self._event_refs.append(signal.event_ref)

    @workflow.query(name="observed_event_count")
    def observed_event_count(self) -> ControlResult:
        return ControlResult(status="running", event_seq=len(self._event_refs))

    @workflow.query(name="leaky_snapshot")
    def leaky_snapshot(self) -> dict[str, str]:
        """A careless Query that returns claim facts.

        No client interceptor can see a Query *result*, so this is the path that
        proves the validating payload converter — not the interceptor — is what
        makes the control-only rule complete. Serialization must refuse it.
        """

        return {
            "insured_name": PRIVACY_SENTINELS["insured_name"],
            "policy_number": PRIVACY_SENTINELS["policy_number"],
        }


@workflow.defn(name="PachaFailingProbeWorkflow", versioning_behavior=VersioningBehavior.PINNED)
class FailingProbeWorkflow:
    """One governed-write-shaped Activity that fails with a classification."""

    @workflow.run
    async def run(self, command: ControlCommand) -> ControlResult:
        policy = load_retry_policies()["governed_external_write"]
        return await workflow.execute_activity(
            failing_activity,
            command,
            start_to_close_timeout=policy.start_to_close_timeout,
            retry_policy=policy.retry_policy,
        )


@workflow.defn(name="PachaHeartbeatProbeWorkflow", versioning_behavior=VersioningBehavior.PINNED)
class HeartbeatProbeWorkflow:
    """A long-compute Activity that heartbeats, fails once and then resumes."""

    @workflow.run
    async def run(self, command: ControlCommand) -> ControlResult:
        policy = load_retry_policies()["long_compute"]
        return await workflow.execute_activity(
            heartbeat_probe_activity,
            command,
            start_to_close_timeout=policy.start_to_close_timeout,
            heartbeat_timeout=policy.heartbeat_timeout,
            retry_policy=policy.retry_policy,
        )


# --- T02: the test-only Signal target -----------------------------------------
#
# T02 registers no production business Workflow, so `TEMPORAL_INTENT_MAPPINGS` is
# empty and there is nothing in `platform/` for a `review.resolved` Signal to
# reach. Routing is still proved end to end, using this test-only Workflow and
# the test-only mapping below. Neither is exported or registered by production
# code; T03 adds the first production mapping beside `DocumentChaseWorkflow`.

#: Stands in for the idempotent Pacha application a real review Activity drives.
#: Keyed by event reference, valued by the number of times it was *applied* —
#: which must stay 1 however many times the event is delivered or Signalled.
APPLIED_REVIEW_EVENTS: dict[str, int] = {}


@activity.defn(name="pacha_test_apply_review")
async def apply_review_activity(command: ControlCommand) -> ControlResult:
    """Apply one resolution reference idempotently, the second layer of de-dup.

    The Workflow already drops a repeated `event_ref` from its own state; this
    is the independent database-side guard, because a Workflow's memory is lost
    on Continue-As-New while Pacha's row is not.
    """

    event_ref = command.event_ref or ""
    if event_ref not in APPLIED_REVIEW_EVENTS:
        APPLIED_REVIEW_EVENTS[event_ref] = 1
    return ControlResult(status="running", run_ref=command.run_ref, event_ref=event_ref)


@workflow.defn(name="PachaTestReviewWaitWorkflow", versioning_behavior=VersioningBehavior.PINNED)
class ReviewWaitWorkflow:
    """Wait for opaque `review_resolved` references and apply each exactly once."""

    def __init__(self) -> None:
        self._pending: list[str] = []
        self._seen: set[str] = set()
        self._applied = 0
        self._closed = False

    @workflow.run
    async def run(self, command: ControlCommand) -> ControlResult:
        policy = load_retry_policies()["db_control"]
        while True:
            await workflow.wait_condition(lambda: bool(self._pending) or self._closed)
            while self._pending:
                event_ref = self._pending.pop(0)
                await workflow.execute_activity(
                    apply_review_activity,
                    ControlCommand(run_ref=command.run_ref, event_ref=event_ref),
                    start_to_close_timeout=policy.start_to_close_timeout,
                    retry_policy=policy.retry_policy,
                )
                self._applied += 1
            if self._closed:
                return ControlResult(
                    status="completed",
                    run_ref=command.run_ref,
                    event_seq=self._applied,
                )

    @workflow.signal(name="review_resolved")
    def review_resolved(self, signal: ControlSignal) -> None:
        """Enqueue an unseen reference. No database call, no decision."""

        if signal.event_ref in self._seen:
            return
        self._seen.add(signal.event_ref)
        self._pending.append(signal.event_ref)

    @workflow.signal(name="claim_terminal")
    def claim_terminal(self, _signal: ControlSignal) -> None:
        self._closed = True


def review_wait_mapping(mapping_type: Any) -> Any:
    """The test-only `review.resolved` -> `ReviewWaitWorkflow` Signal mapping.

    Takes `TemporalIntentMapping` as an argument so this module keeps its T01
    import surface and does not depend on `orchestration.starter`.

    The builder resolves the target from the event's own `agent_run_id`, and
    returns `None` when the payload carries none — the "valid event with no
    Temporal target" case, which must be acknowledged rather than retried.
    """

    from orchestration.contracts import ControlSignal as _ControlSignal
    from orchestration.ids import agent_workflow_ref

    def build(event: Any):
        run_ref = (event.payload or {}).get("agent_run_id")
        if not isinstance(run_ref, str) or not run_ref:
            return None
        return agent_workflow_ref(run_ref)

    return mapping_type(
        event_type="review.resolved",
        workflow_type=ReviewWaitWorkflow,
        workflow_id_builder=build,
        action="signal",
        signal_name="review_resolved",
        control_contract_type=_ControlSignal,
    )
