"""T01 unit suite — configuration, Codec, control contracts, IDs and policies.

These are the master plan's section 22.1 checks. Anything needing a real
Temporal server lives in `tests/integration/test_temporal_orchestration.py`.

Async surfaces are driven with `asyncio.run` rather than an async test plugin:
T01 adds exactly the two runtime dependencies the plan lists and no test-only
ones.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import subprocess
import sys
from typing import Any

import pytest
import yaml
from temporalio.api.common.v1 import Payload
from temporalio.common import VersioningBehavior
from temporalio.converter import DataConverter

from orchestration.client import AwsSecretsManagerProvider, build_temporal_client
from orchestration.codec import (
    CODEC_ENCODING,
    GeneratedDataKey,
    KmsDataKeyProvider,
    PachaPayloadCodec,
    StaticDataKeyProvider,
    build_data_converter,
)
from orchestration.config import WORKER_ROLES, TemporalConfig
from orchestration.contracts import (
    CONTROL_FIELDS,
    CONTROL_STATUSES,
    FORBIDDEN_CATEGORIES,
    MAX_CONTROL_NESTING_DEPTH,
    MAX_CONTROL_PAYLOAD_BYTES,
    MAX_CONTROL_STRING_BYTES,
    ControlCommand,
    ControlHeartbeat,
    ControlPayloadConverter,
    ControlResult,
    ControlSignal,
    control_payload_size,
    load_control_registries,
    scan_forbidden_categories,
    validate_control_collection,
    validate_control_field,
    validate_control_payload,
)
from orchestration.errors import (
    NON_RETRYABLE_FAILURE_TYPES,
    TEMPORAL_FAILURE_TYPES,
    CodecError,
    ConfigurationError,
    ControlContractError,
    HistoryPrivacyError,
    RetryPolicyError,
    WorkflowIdError,
    sanitised_application_error,
)
from orchestration.history import assert_no_sentinels, find_sentinels
from orchestration.ids import (
    WorkflowRef,
    agent_workflow_ref,
    approval_pack_workflow_ref,
    assessment_workflow_ref,
    chase_workflow_ref,
    docintel_workflow_ref,
    intake_workflow_ref,
    parse_workflow_ref,
    projection_workflow_ref,
)
from orchestration.policies import POLICY_CEILINGS, load_retry_policies, parse_duration
from orchestration.worker import (
    ROLE_ACTIVITY_CONCURRENCY,
    WORKER_GRACEFUL_SHUTDOWN,
    WORKFLOW_SAFE_MODULES,
    build_worker,
)
from support.temporal import (
    CHECKLIST_REF,
    CLAIM_REF,
    CLOUD_KMS_KEY_ARN,
    DOCUMENT_REF,
    EVENT_REF,
    PRIVACY_SENTINELS,
    PROJECTION_REF,
    RUN_REF,
    STATIC_CODEC_KEY,
    TRIGGER_EVENT_REF,
    WORKFLOW_RUN_REF,
    FailingKmsClient,
    FakeKmsClient,
    FakeSecretProvider,
    FakeSecretsManagerClient,
    cloud_config,
    cloud_environ,
    local_config,
    local_environ,
)

_REPO = pathlib.Path(__file__).resolve().parents[2]


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _payload(body: bytes = b"control-only") -> Payload:
    return Payload(metadata={"encoding": b"json/plain"}, data=body)


def _static_codec(namespace: str = "default", **overrides: Any) -> PachaPayloadCodec:
    provider = StaticDataKeyProvider(STATIC_CODEC_KEY)
    kwargs: dict[str, Any] = {
        "namespace": namespace,
        "approved_key_arns": [provider.key_arn],
    }
    kwargs.update(overrides)
    return PachaPayloadCodec(provider, **kwargs)


# --- section 6: configuration -------------------------------------------------


def test_local_environment_is_accepted():
    config = TemporalConfig.from_environ(local_environ())
    assert config.mode == "local"
    assert config.address == "localhost:7233"
    assert config.is_cloud is False
    assert config.is_production_like is False


def test_cloud_environment_is_accepted():
    config = TemporalConfig.from_environ(cloud_environ())
    assert config.is_cloud is True
    assert config.is_production_like is True
    assert config.region == "af-south-1"


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_local_mode_is_refused_in_production_environments(env):
    environ = local_environ(PACHA_ENV=env, PACHA_TEMPORAL_QUEUE_PREFIX=f"pacha-{env}")
    with pytest.raises(ConfigurationError, match="refused"):
        TemporalConfig.from_environ(environ)


@pytest.mark.parametrize(
    "variable",
    [
        "PACHA_ENV",
        "PACHA_TEMPORAL_MODE",
        "PACHA_TEMPORAL_NAMESPACE",
        "PACHA_TEMPORAL_QUEUE_PREFIX",
        "PACHA_BUILD_ID",
    ],
)
def test_every_always_required_variable_is_required(variable):
    environ = local_environ()
    environ.pop(variable)
    with pytest.raises(ConfigurationError, match=variable):
        TemporalConfig.from_environ(environ)


@pytest.mark.parametrize(
    "variable",
    [
        "PACHA_TEMPORAL_ADDRESS",
        "PACHA_TEMPORAL_REGION",
        "PACHA_TEMPORAL_TLS_CERT_SECRET_ARN",
        "PACHA_TEMPORAL_TLS_KEY_SECRET_ARN",
        "PACHA_TEMPORAL_KMS_KEY_ARN",
    ],
)
def test_every_cloud_variable_is_required_in_cloud_mode(variable):
    environ = cloud_environ()
    environ.pop(variable)
    with pytest.raises(ConfigurationError, match=variable):
        TemporalConfig.from_environ(environ)


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("PACHA_ENV", "production"),
        ("PACHA_TEMPORAL_MODE", "hybrid"),
        ("PACHA_TEMPORAL_NAMESPACE", "has space"),
        ("PACHA_TEMPORAL_QUEUE_PREFIX", "pacha-prod"),
        ("PACHA_BUILD_ID", "not-a-sha"),
        ("PACHA_WORKER_ROLE", "reaper"),
    ],
)
def test_malformed_always_present_variables_are_refused(variable, value):
    with pytest.raises(ConfigurationError):
        TemporalConfig.from_environ(local_environ(**{variable: value}))


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("PACHA_TEMPORAL_ADDRESS", "temporal.example"),
        ("PACHA_TEMPORAL_REGION", "Cape Town"),
        ("PACHA_TEMPORAL_TLS_CERT_SECRET_ARN", "arn:aws:s3:::pacha-certs"),
        ("PACHA_TEMPORAL_TLS_KEY_SECRET_ARN", "not-an-arn"),
        ("PACHA_TEMPORAL_KMS_KEY_ARN", "arn:aws:kms:af-south-1:123456789012:key/short"),
    ],
)
def test_malformed_cloud_variables_are_refused(variable, value):
    with pytest.raises(ConfigurationError):
        TemporalConfig.from_environ(cloud_environ(**{variable: value}))


def test_implicit_default_namespace_is_refused_outside_dev_and_test():
    with pytest.raises(ConfigurationError, match="explicit"):
        TemporalConfig.from_environ(cloud_environ(PACHA_TEMPORAL_NAMESPACE="default"))


def test_default_namespace_is_permitted_in_test():
    assert TemporalConfig.from_environ(local_environ()).namespace == "default"


def test_queue_prefix_must_be_exactly_pacha_env():
    with pytest.raises(ConfigurationError, match="pacha-test"):
        TemporalConfig.from_environ(local_environ(PACHA_TEMPORAL_QUEUE_PREFIX="pacha"))


def test_worker_role_is_required_only_when_starting_a_worker():
    assert TemporalConfig.from_environ(local_environ()).worker_role is None
    with pytest.raises(ConfigurationError, match="PACHA_WORKER_ROLE"):
        TemporalConfig.from_environ(local_environ(), require_worker_role=True)


def test_task_queue_and_deployment_names_follow_sections_seven_and_eight():
    config = local_config()
    assert config.task_queue("control") == "pacha-test-control-v1"
    assert config.deployment_name("docintel") == "pacha-test-docintel"
    with pytest.raises(ConfigurationError):
        config.task_queue("reaper")
    with pytest.raises(ConfigurationError):
        config.task_queue()
    with pytest.raises(ConfigurationError):
        config.deployment_name("reaper")
    with pytest.raises(ConfigurationError):
        config.deployment_name()


def test_direct_construction_is_validated_exactly_as_the_environment_is():
    base = dict(
        env="test",
        mode="local",
        address="localhost:7233",
        namespace="default",
        queue_prefix="pacha-test",
        build_id="a" * 40,
    )
    assert TemporalConfig(**base).env == "test"
    with pytest.raises(ConfigurationError, match="PACHA_ENV"):
        TemporalConfig(**{**base, "env": "production"})
    with pytest.raises(ConfigurationError, match="PACHA_TEMPORAL_MODE"):
        TemporalConfig(**{**base, "mode": "hybrid"})
    with pytest.raises(ConfigurationError, match="PACHA_WORKER_ROLE"):
        TemporalConfig(**base, worker_role="reaper")


# --- section 6: mTLS and the Secrets Manager seam ------------------------------


def test_secrets_manager_provider_reads_both_arns_and_never_persists_them():
    config = cloud_config()
    provider = FakeSecretProvider(
        {
            config.tls_cert_secret_arn: b"-----BEGIN CERTIFICATE-----",
            config.tls_key_secret_arn: b"-----BEGIN PRIVATE KEY-----",
        }
    )
    from orchestration.client import _build_tls_config

    tls = _build_tls_config(config, provider)
    assert provider.requested == [config.tls_cert_secret_arn, config.tls_key_secret_arn]
    assert tls.client_cert == b"-----BEGIN CERTIFICATE-----"
    assert tls.client_private_key == b"-----BEGIN PRIVATE KEY-----"


def test_aws_secrets_manager_provider_returns_secret_string_bytes():
    provider = AwsSecretsManagerProvider(client=FakeSecretsManagerClient({"arn:x": "pem"}))
    assert provider.secret_bytes("arn:x") == b"pem"


def test_secrets_manager_failure_is_reported_without_the_raw_aws_text():
    class Broken:
        def get_secret_value(self, **_: Any) -> dict[str, str]:
            raise RuntimeError("AccessDenied for arn:aws:secretsmanager:...:pacha/temporal/key")

    provider = AwsSecretsManagerProvider(client=Broken())
    with pytest.raises(ConfigurationError) as caught:
        provider.secret_bytes("arn:x")
    assert "AccessDenied" not in str(caught.value)


def test_a_binary_secret_is_returned_as_bytes():
    class Binary:
        def get_secret_value(self, *, SecretId: str) -> dict[str, Any]:  # noqa: N803
            return {"SecretBinary": bytearray(b"der-bytes")}

    assert AwsSecretsManagerProvider(client=Binary()).secret_bytes("arn:x") == b"der-bytes"


def test_an_empty_secret_is_refused_rather_than_used():
    provider = AwsSecretsManagerProvider(client=FakeSecretsManagerClient({"arn:x": ""}))
    with pytest.raises(ConfigurationError, match="empty"):
        provider.secret_bytes("arn:x")


def test_tls_construction_refuses_missing_arns_and_empty_material():
    from orchestration.client import _build_tls_config

    config = cloud_config()
    local = local_config()
    with pytest.raises(ConfigurationError, match="mTLS Secrets Manager ARNs"):
        _build_tls_config(local, FakeSecretProvider({}))
    empty = FakeSecretProvider(
        {config.tls_cert_secret_arn: b"", config.tls_key_secret_arn: b"key"}
    )
    with pytest.raises(ConfigurationError, match="empty"):
        _build_tls_config(config, empty)


def test_injected_secret_provider_is_refused_in_production_like_environments():
    with pytest.raises(ConfigurationError, match="refused in staging"):
        _run(
            build_temporal_client(
                cloud_config(),
                secret_provider=FakeSecretProvider({}),
            )
        )


# --- section 11: Payload Codec ------------------------------------------------


def test_codec_round_trip_restores_the_original_payload():
    codec = _static_codec()
    original = [_payload(b"first"), _payload(b"second")]
    encoded = _run(codec.encode(original))
    assert [p.metadata["encoding"] for p in encoded] == [CODEC_ENCODING, CODEC_ENCODING]
    decoded = _run(codec.decode(encoded))
    assert [p.data for p in decoded] == [b"first", b"second"]
    assert [dict(p.metadata) for p in decoded] == [dict(p.metadata) for p in original]


def test_identical_payloads_get_different_nonces_and_different_ciphertext():
    codec = _static_codec()
    encoded = _run(codec.encode([_payload(b"same"), _payload(b"same")]))
    assert encoded[0].metadata["pacha-nonce"] != encoded[1].metadata["pacha-nonce"]
    assert encoded[0].data != encoded[1].data


def test_one_generate_data_key_call_per_encode_batch():
    arn = "arn:aws:kms:af-south-1:123456789012:key/3f2504e0-4f89-41d3-9a0c-0305e82c3301"
    kms = FakeKmsClient(key_arn=arn)
    codec = PachaPayloadCodec(
        KmsDataKeyProvider(arn, client=kms),
        namespace="pacha-staging.a1b2c",
        approved_key_arns=[arn],
    )
    encoded = _run(codec.encode([_payload(b"a"), _payload(b"b"), _payload(b"c")]))
    assert kms.generate_calls == 1
    _run(codec.decode(encoded))
    assert kms.decrypt_calls == 1  # one Decrypt per wrapped-key group


def test_tampered_ciphertext_is_refused():
    codec = _static_codec()
    encoded = _run(codec.encode([_payload()]))
    encoded[0].data = bytes([encoded[0].data[0] ^ 0xFF]) + encoded[0].data[1:]
    with pytest.raises(CodecError, match="authentication"):
        _run(codec.decode(encoded))


def test_tampered_nonce_metadata_is_refused():
    codec = _static_codec()
    encoded = _run(codec.encode([_payload()]))
    encoded[0].metadata["pacha-nonce"] = bytes(12)
    with pytest.raises(CodecError, match="authentication"):
        _run(codec.decode(encoded))


def test_a_payload_cannot_be_replayed_into_another_namespace():
    """The AAD binds ciphertext to `{namespace}|{kms_key_arn}` (section 11.3)."""

    encoded = _run(_static_codec(namespace="pacha-staging.a1b2c").encode([_payload()]))
    with pytest.raises(CodecError, match="authentication"):
        _run(_static_codec(namespace="pacha-prod.a1b2c").decode(encoded))


def test_malformed_nonce_length_is_refused():
    codec = _static_codec()
    encoded = _run(codec.encode([_payload()]))
    encoded[0].metadata["pacha-nonce"] = b"short"
    with pytest.raises(CodecError, match="malformed nonce"):
        _run(codec.decode(encoded))


def test_unknown_codec_version_is_refused():
    codec = _static_codec()
    encoded = _run(codec.encode([_payload()]))
    encoded[0].metadata["pacha-codec-version"] = b"2"
    with pytest.raises(CodecError, match="unknown Pacha Codec version"):
        _run(codec.decode(encoded))


def test_missing_codec_metadata_is_refused():
    codec = _static_codec()
    encoded = _run(codec.encode([_payload()]))
    del encoded[0].metadata["pacha-wrapped-key"]
    with pytest.raises(CodecError, match="missing required Codec metadata"):
        _run(codec.decode(encoded))


def test_unapproved_key_arn_is_refused_on_decode():
    encoded = _run(_static_codec().encode([_payload()]))
    stricter = PachaPayloadCodec(
        StaticDataKeyProvider(STATIC_CODEC_KEY),
        namespace="default",
        approved_key_arns=["arn:aws:kms:af-south-1:123456789012:alias/other"],
    )
    with pytest.raises(CodecError, match="approved Codec allowlist"):
        _run(stricter.decode(encoded))


def test_unapproved_key_arn_is_refused_on_encode():
    codec = PachaPayloadCodec(
        StaticDataKeyProvider(STATIC_CODEC_KEY),
        namespace="default",
        approved_key_arns=["arn:aws:kms:af-south-1:123456789012:alias/other"],
    )
    with pytest.raises(CodecError, match="approved Codec allowlist"):
        _run(codec.encode([_payload()]))


def test_kms_failure_refuses_rather_than_degrading():
    arn = "arn:aws:kms:af-south-1:123456789012:alias/pacha-temporal"
    codec = PachaPayloadCodec(
        KmsDataKeyProvider(arn, client=FailingKmsClient()),
        namespace="pacha-staging.a1b2c",
        approved_key_arns=[arn],
    )
    with pytest.raises(CodecError, match="GenerateDataKey"):
        _run(codec.encode([_payload()]))


def test_plaintext_payloads_are_never_accepted_on_decode():
    with pytest.raises(CodecError, match="plaintext is never accepted"):
        _run(_static_codec().decode([_payload()]))


def _production_like_config(env: str) -> TemporalConfig:
    return TemporalConfig.from_environ(
        cloud_environ(PACHA_ENV=env, PACHA_TEMPORAL_QUEUE_PREFIX=f"pacha-{env}")
    )


class _CustomDataKeyProvider:
    """A non-static injected provider — the production-bypass shape."""

    key_arn = "arn:aws:kms:af-south-1:123456789012:key/3f2504e0-4f89-41d3-9a0c-0305e82c3301"

    async def generate_data_key(self):
        return GeneratedDataKey(
            key_arn=self.key_arn, plaintext=STATIC_CODEC_KEY, wrapped=b"custom"
        )

    async def unwrap_data_key(self, key_arn: str, wrapped: bytes) -> bytes:
        return STATIC_CODEC_KEY


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_static_codec_keys_are_refused_in_staging_and_prod(env):
    config = _production_like_config(env)
    with pytest.raises(CodecError, match=f"refused in {env}"):
        build_data_converter(config, data_key_provider=StaticDataKeyProvider(STATIC_CODEC_KEY))


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_every_injected_data_key_provider_is_refused_in_production_not_only_the_static_one(env):
    """Regression: type was the discriminator, so a custom provider walked past.

    A bespoke provider can return any key material at all, including a constant.
    The environment, not the class, decides whether a seam is permitted.
    """

    config = _production_like_config(env)
    with pytest.raises(CodecError, match=f"refused in {env}"):
        build_data_converter(config, data_key_provider=_CustomDataKeyProvider())


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_an_injected_key_allowlist_is_refused_in_production(env):
    config = _production_like_config(env)
    other_key = (
        "arn:aws:kms:af-south-1:123456789012:key/"
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    )
    with pytest.raises(CodecError, match="key allowlist"):
        build_data_converter(config, approved_key_arns=[other_key])


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_the_client_refuses_both_injected_seams_in_production(env):
    config = _production_like_config(env)
    with pytest.raises(ConfigurationError, match="data_key_provider"):
        _run(build_temporal_client(config, data_key_provider=_CustomDataKeyProvider()))
    with pytest.raises(ConfigurationError, match="secret_provider"):
        _run(build_temporal_client(config, secret_provider=FakeSecretProvider({})))


def test_injected_providers_remain_available_in_dev_and_test():
    converter = build_data_converter(
        local_config(),
        data_key_provider=_CustomDataKeyProvider(),
        approved_key_arns=[_CustomDataKeyProvider.key_arn],
    )
    assert isinstance(converter.payload_codec, PachaPayloadCodec)


# --- section 11 regression: KMS key identity ------------------------------------


def test_an_alias_kms_arn_is_refused_by_configuration():
    """Regression: an alias-addressed key breaks encoding at runtime.

    `GenerateDataKey` echoes the canonical key ARN in `KeyId` however the key was
    addressed, so a Codec allowlist pinned to an alias rejects its own data key.
    """

    alias = "arn:aws:kms:af-south-1:123456789012:alias/pacha-temporal"
    with pytest.raises(ConfigurationError, match="alias"):
        TemporalConfig.from_environ(cloud_environ(PACHA_TEMPORAL_KMS_KEY_ARN=alias))


def test_the_configured_key_arn_is_the_arn_kms_echoes_back():
    kms = FakeKmsClient(key_arn=CLOUD_KMS_KEY_ARN)
    codec = PachaPayloadCodec(
        KmsDataKeyProvider(CLOUD_KMS_KEY_ARN, client=kms),
        namespace="pacha-staging.a1b2c",
        approved_key_arns=[CLOUD_KMS_KEY_ARN],
    )
    encoded = _run(codec.encode([_payload(b"control")]))
    assert kms.requested_key_ids == [CLOUD_KMS_KEY_ARN]
    assert encoded[0].metadata["pacha-kms-key-arn"].decode() == CLOUD_KMS_KEY_ARN
    assert [p.data for p in _run(codec.decode(encoded))] == [b"control"]


def test_an_alias_pinned_allowlist_reproduces_the_reported_encode_failure():
    """The exact failure the key-ARN-only rule exists to prevent."""

    alias = "arn:aws:kms:af-south-1:123456789012:alias/pacha-temporal"
    kms = FakeKmsClient(key_arn=CLOUD_KMS_KEY_ARN)
    codec = PachaPayloadCodec(
        KmsDataKeyProvider(alias, client=kms),
        namespace="pacha-staging.a1b2c",
        approved_key_arns=[alias],
    )
    with pytest.raises(CodecError, match="approved Codec allowlist"):
        _run(codec.encode([_payload()]))
    assert kms.requested_key_ids == [alias]  # addressed by alias...
    assert kms.key_arn == CLOUD_KMS_KEY_ARN  # ...answered with the key ARN


def test_a_static_key_must_be_thirty_two_bytes():
    with pytest.raises(CodecError, match="32 bytes"):
        StaticDataKeyProvider(b"short")


def test_no_key_source_means_no_converter_rather_than_a_plaintext_one():
    with pytest.raises(CodecError, match="no Codec key source"):
        build_data_converter(local_config())


def test_a_kms_data_key_of_the_wrong_size_is_refused():
    arn = "arn:aws:kms:af-south-1:123456789012:alias/pacha-temporal"

    class ShortKey(FakeKmsClient):
        def generate_data_key(self, **kwargs: Any) -> dict[str, Any]:
            response = super().generate_data_key(**kwargs)
            response["Plaintext"] = b"too-short"
            return response

    provider = KmsDataKeyProvider(arn, client=ShortKey(key_arn=arn))
    with pytest.raises(CodecError, match="256 bits"):
        _run(provider.generate_data_key())


def test_a_kms_decrypt_returning_the_wrong_size_is_refused():
    arn = "arn:aws:kms:af-south-1:123456789012:alias/pacha-temporal"

    class ShortKey(FakeKmsClient):
        def decrypt(self, **kwargs: Any) -> dict[str, Any]:
            super().decrypt(**kwargs)
            return {"Plaintext": b"too-short"}

    provider = KmsDataKeyProvider(arn, client=ShortKey(key_arn=arn))
    with pytest.raises(CodecError, match="256 bits"):
        _run(provider.unwrap_data_key(arn, b"wrapped:" + STATIC_CODEC_KEY))


def test_kms_decrypt_failure_refuses_rather_than_degrading():
    arn = "arn:aws:kms:af-south-1:123456789012:alias/pacha-temporal"
    provider = KmsDataKeyProvider(arn, client=FailingKmsClient())
    with pytest.raises(CodecError, match="Decrypt failed"):
        _run(provider.unwrap_data_key(arn, b"wrapped"))


def test_the_static_provider_only_unwraps_its_own_data_key():
    provider = StaticDataKeyProvider(STATIC_CODEC_KEY)
    with pytest.raises(CodecError, match="cannot unwrap"):
        _run(provider.unwrap_data_key("arn:other", b"static"))


def test_a_malformed_key_arn_in_metadata_is_refused():
    codec = _static_codec()
    encoded = _run(codec.encode([_payload()]))
    encoded[0].metadata["pacha-kms-key-arn"] = b"\xff\xfe"
    with pytest.raises(CodecError, match="malformed key ARN"):
        _run(codec.decode(encoded))


def test_a_codec_with_an_empty_allowlist_cannot_be_built():
    with pytest.raises(CodecError, match="approved KMS key ARN"):
        PachaPayloadCodec(
            StaticDataKeyProvider(STATIC_CODEC_KEY), namespace="default", approved_key_arns=[]
        )


def test_an_empty_batch_is_a_no_op_in_both_directions():
    codec = _static_codec()
    assert _run(codec.encode([])) == []
    assert _run(codec.decode([])) == []


def test_an_injected_provider_needs_an_approved_key_allowlist():
    class BareProvider:
        async def generate_data_key(self):  # pragma: no cover - never reached
            raise AssertionError

        async def unwrap_data_key(self, key_arn, wrapped):  # pragma: no cover
            raise AssertionError

    with pytest.raises(CodecError, match="approved key ARN allowlist"):
        build_data_converter(local_config(), data_key_provider=BareProvider())


def test_an_explicit_allowlist_is_honoured():
    converter = build_data_converter(
        local_config(),
        data_key_provider=StaticDataKeyProvider(STATIC_CODEC_KEY),
        approved_key_arns=["static/pacha-test-codec-key"],
    )
    assert isinstance(converter.payload_codec, PachaPayloadCodec)


def test_production_builds_the_kms_provider_from_the_configured_key(monkeypatch):
    config = cloud_config()
    built: dict[str, Any] = {}

    def fake_build_client() -> Any:
        built["called"] = True
        return FakeKmsClient(key_arn=config.kms_key_arn)

    monkeypatch.setattr(KmsDataKeyProvider, "_build_client", staticmethod(fake_build_client))
    converter = build_data_converter(config)
    assert isinstance(converter.payload_codec, PachaPayloadCodec)
    assert built["called"] is True


def test_kms_standard_retry_configuration_caps_total_attempts_at_three(monkeypatch):
    import boto3

    captured: dict[str, Any] = {}

    def fake_client(service: str, *, config: Any) -> object:
        captured["service"] = service
        captured["config"] = config
        return object()

    monkeypatch.setattr(boto3, "client", fake_client)
    KmsDataKeyProvider._build_client()

    assert captured["service"] == "kms"
    retries = captured["config"].retries
    assert retries["mode"] == "standard"
    assert retries["total_max_attempts"] == 3
    assert "max_attempts" not in retries


def test_every_converter_carries_the_pacha_codec():
    converter = build_data_converter(
        local_config(), data_key_provider=StaticDataKeyProvider(STATIC_CODEC_KEY)
    )
    assert isinstance(converter, DataConverter)
    assert isinstance(converter.payload_codec, PachaPayloadCodec)


# --- section 10: control-only payload contract ---------------------------------


def test_the_allowlist_is_exactly_the_twenty_declared_fields():
    assert len(CONTROL_FIELDS) == 20
    assert len(set(CONTROL_FIELDS)) == 20


def test_a_valid_control_command_and_result_round_trip_their_fields():
    command = ControlCommand(
        run_ref=RUN_REF,
        claim_ref=CLAIM_REF,
        workflow_ref=f"pacha.chase.{CHECKLIST_REF}",
        workflow_run_ref=WORKFLOW_RUN_REF,
        checklist_ref=CHECKLIST_REF,
        write_id=f"chase:{CHECKLIST_REF.lower()}:3",
        step_id="ingest",
        pack_version="1.0.0",
        attempt_no=0,
    )
    assert command.as_control_mapping()["write_id"].startswith("chase:")
    result = ControlResult(status="awaiting_review", run_ref=RUN_REF, wake_at_epoch_ms=0)
    assert result.status in CONTROL_STATUSES


@pytest.mark.parametrize("field", sorted(set(CONTROL_FIELDS)))
@pytest.mark.parametrize("category", sorted(PRIVACY_SENTINELS))
def test_no_control_field_accepts_any_forbidden_data_category(field, category):
    """Every seeded PII sentinel is refused by every allowlisted field.

    The closure is structural: a field that only accepts a ULID, a UUID, a
    digest, a registry token or a bounded integer has nowhere to put a name, a
    policy number, a plate, bank data, a money figure, document text, a
    credential or a sentence.
    """

    with pytest.raises(ControlContractError):
        validate_control_field(field, PRIVACY_SENTINELS[category])


_CATEGORY_SAMPLES = {
    "party_details": "insured",
    "postal_address": "po_box",
    "policy_or_registration": "kda812x",
    "identity_or_bank": "kra",
    "document_or_extracted_fact": "extracted",
    "money_or_settlement": "kes",
    "narrative_or_prose": "two words",
    "recipient_list": "recipients",
    "model_payload": "prompt",
    "credential": "token",
    "raw_error": "traceback",
}


@pytest.mark.parametrize("category", sorted(_CATEGORY_SAMPLES))
def test_the_second_barrier_names_every_forbidden_category(category):
    assert scan_forbidden_categories(_CATEGORY_SAMPLES[category]) == category


def test_the_second_barrier_covers_every_declared_category():
    assert set(_CATEGORY_SAMPLES) == {category for category, _ in FORBIDDEN_CATEGORIES}


def test_the_second_barrier_never_fires_on_a_valid_registry_token():
    registries = load_control_registries()
    corpus = (
        set(registries.step_ids)
        | set(registries.pack_versions)
        | set(CONTROL_STATUSES)
        | {"chase", "icon.claim_register", "edms.upload"}
    )
    assert {token for token in corpus if scan_forbidden_categories(token)} == set()


@pytest.mark.parametrize(
    "field",
    ["run_ref", "claim_ref", "trigger_event_ref", "event_ref", "review_event_ref",
     "document_ref", "checklist_ref", "projection_ref"],
)
@pytest.mark.parametrize(
    "value",
    ["01JZ8Q9R7K4M2N6P8T0V3W5Y7", "01jz8q9r7k4m2n6p8t0v3w5y7z", "01JZ8Q9R7K4M2N6P8T0V3W5YIZ", ""],
)
def test_invalid_ulids_are_refused(field, value):
    with pytest.raises(ControlContractError, match="ULID"):
        validate_control_field(field, value)


def test_invalid_payload_hash_is_refused():
    with pytest.raises(ControlContractError, match="hexadecimal"):
        validate_control_field("payload_hash", "A" * 64)
    with pytest.raises(ControlContractError, match="hexadecimal"):
        validate_control_field("payload_hash", "ab" * 31)
    validate_control_field("payload_hash", "ab" * 32)


def test_invalid_workflow_run_uuid_is_refused():
    with pytest.raises(ControlContractError, match="UUID"):
        validate_control_field("workflow_run_ref", WORKFLOW_RUN_REF.replace("-", ""))
    validate_control_field("workflow_run_ref", WORKFLOW_RUN_REF)


@pytest.mark.parametrize(
    "value",
    [
        "chase",  # no opaque segment at all
        "Chase:01jz8q9r7k4m2n6p8t0v3w5y7z:1",  # uppercase operation
        f"chase:{CHECKLIST_REF}:1",  # ULID segment must be lowercase-opaque
        "chase:reminder-two:1",  # a segment that is neither ULID nor integer
        "chase:01jz8q9r7k4m2n6p8t0v3w5y7z:01",  # padded integer
    ],
)
def test_invalid_write_ids_are_refused(value):
    with pytest.raises(ControlContractError):
        validate_control_field("write_id", value)


def test_a_valid_write_id_is_an_operation_plus_opaque_segments():
    validate_control_field("write_id", f"chase:{CHECKLIST_REF.lower()}:0")
    validate_control_field("write_id", f"icon.claim_register:{CLAIM_REF.lower()}:12")


def test_unregistered_status_step_and_pack_version_are_refused():
    with pytest.raises(ControlContractError, match="run status"):
        validate_control_field("status", "reaped")
    with pytest.raises(ControlContractError, match="COP step"):
        validate_control_field("step_id", "not_a_step")
    with pytest.raises(ControlContractError, match="pack version"):
        validate_control_field("pack_version", "9.9.9")


def test_schedule_refs_must_match_the_stable_section_sixteen_form():
    validate_control_field("schedule_ref", "pacha-prod-outbox-drain-v1")
    with pytest.raises(ControlContractError, match="Schedule-ID"):
        validate_control_field("schedule_ref", "pacha-prod-outbox-drain")


@pytest.mark.parametrize("field", ["event_seq", "wake_at_epoch_ms", "timer_seconds", "attempt_no"])
@pytest.mark.parametrize("value", [-1, "3", 3.0, True])
def test_integer_fields_reject_negatives_and_non_integers(field, value):
    with pytest.raises(ControlContractError):
        validate_control_field(field, value)


def test_an_unknown_field_name_is_refused_outright():
    with pytest.raises(ControlContractError, match="allowlisted"):
        validate_control_field("insured_name", "anything")


def test_oversized_strings_are_refused():
    with pytest.raises(ControlContractError, match="UTF-8 bytes"):
        validate_control_field("write_id", "a" * (MAX_CONTROL_STRING_BYTES + 1))
    assert MAX_CONTROL_PAYLOAD_BYTES == 8 * 1024


def test_arbitrary_structures_may_not_cross_the_boundary():
    with pytest.raises(ControlContractError, match="not an allowlisted control field"):
        validate_control_payload({"insured_name": "Wanjiru Kamau"})
    with pytest.raises(ControlContractError, match="bare strings"):
        validate_control_payload("KES 1,482,500.00")
    with pytest.raises(ControlContractError, match="not a control contract"):
        validate_control_payload(object())
    with pytest.raises(ControlContractError, match="not a control contract"):
        validate_control_payload(1.5)
    validate_control_payload([ControlSignal(event_ref=EVENT_REF)])


@pytest.mark.parametrize("value", [None, True, False, 0, 42, 148_250_000, [148_250_000]])
def test_unnamed_scalars_are_refused_because_they_can_carry_forbidden_facts(value):
    with pytest.raises(ControlContractError, match="unnamed scalars"):
        validate_control_payload(value)


# --- section 10 regression: the 8 KiB limit binds the whole collection ------------


def _signals(count: int) -> list[ControlSignal]:
    return [ControlSignal(event_ref=EVENT_REF) for _ in range(count)]


def test_control_payload_size_is_the_exact_canonical_json_length():
    command = ControlCommand(run_ref=RUN_REF, claim_ref=CLAIM_REF, step_id="ingest")
    expected = len(
        json.dumps([command.as_control_mapping()], separators=(",", ":")).encode("utf-8")
    )
    assert control_payload_size([command]) == expected


def test_the_eight_kib_limit_binds_the_collection_not_each_argument():
    """Regression: arguments were validated independently, so any number of
    individually legal ones could add up to an illegal payload. The CTO passed
    17,200 bytes through the old path."""

    item_size = len(
        json.dumps(ControlSignal(event_ref=EVENT_REF).as_control_mapping(), separators=(",", ":"))
        .encode("utf-8")
    )
    over = _signals((MAX_CONTROL_PAYLOAD_BYTES - 1) // (item_size + 1) + 1)
    canonical_size = len(
        json.dumps(
            [signal.as_control_mapping() for signal in over],
            separators=(",", ":"),
        ).encode("utf-8")
    )
    assert canonical_size > MAX_CONTROL_PAYLOAD_BYTES
    with pytest.raises(ControlContractError, match="exceeds 8192 bytes"):
        validate_control_collection(over)


def test_an_unbounded_sequence_argument_is_refused():
    with pytest.raises(ControlContractError, match="exceeds 8192 bytes"):
        validate_control_collection([_signals(5_000)])


def test_the_limit_boundary_is_exact():
    item_size = len(
        json.dumps(ControlSignal(event_ref=EVENT_REF).as_control_mapping(), separators=(",", ":"))
        .encode("utf-8")
    )
    # Complete JSON collection size is `[]` + each item + commas.
    fitting = (MAX_CONTROL_PAYLOAD_BYTES - 1) // (item_size + 1)
    payload = _signals(fitting)
    assert control_payload_size(payload) == len(
        json.dumps(
            [signal.as_control_mapping() for signal in payload],
            separators=(",", ":"),
        ).encode("utf-8")
    )
    assert control_payload_size(payload) <= MAX_CONTROL_PAYLOAD_BYTES
    validate_control_collection(payload)
    with pytest.raises(ControlContractError, match="exceeds 8192 bytes"):
        validate_control_collection(_signals(fitting + 1))


def test_a_control_payload_may_not_be_an_arbitrarily_deep_structure():
    nested: Any = [ControlSignal(event_ref=EVENT_REF)]
    for _ in range(MAX_CONTROL_NESTING_DEPTH + 1):
        nested = [nested]
    with pytest.raises(ControlContractError, match="nested deeper"):
        validate_control_payload(nested)


# --- section 10 regression: the validating payload converter ---------------------


def test_the_converter_refuses_every_value_the_contract_forbids():
    """Regression: a Workflow could return an arbitrary dictionary and the Codec
    would faithfully encrypt it. The converter sits ahead of serialization on
    every payload path, so no result, Query reply or heartbeat escapes it."""

    converter = ControlPayloadConverter()
    for forbidden in (
        {"insured_name": PRIVACY_SENTINELS["insured_name"]},
        PRIVACY_SENTINELS["narrative"],
        1.5,
        b"raw",
        object(),
        {"run_ref": PRIVACY_SENTINELS["policy_number"]},
    ):
        with pytest.raises(ControlContractError):
            converter.to_payloads([forbidden])


def test_the_converter_serialises_every_permitted_value():
    converter = ControlPayloadConverter()
    assert len(converter.to_payloads([ControlResult(status="completed")])) == 1
    assert len(converter.to_payloads([{"run_ref": RUN_REF}])) == 1


def test_the_converter_enforces_the_collection_limit_not_the_argument_limit():
    converter = ControlPayloadConverter()
    with pytest.raises(ControlContractError, match="exceeds 8192 bytes"):
        converter.to_payloads(_signals(MAX_CONTROL_PAYLOAD_BYTES))


def test_a_context_scoped_converter_clone_still_validates():
    """`with_context` clones through a nullary constructor, so validation
    cannot be shed part-way through a call."""

    converter = ControlPayloadConverter()
    for candidate in (converter, converter.with_context(None)):
        assert isinstance(candidate, ControlPayloadConverter)


def test_every_data_converter_installs_the_validating_payload_converter():
    converter = build_data_converter(
        local_config(), data_key_provider=StaticDataKeyProvider(STATIC_CODEC_KEY)
    )
    assert converter.payload_converter_class is ControlPayloadConverter
    assert isinstance(converter.payload_converter, ControlPayloadConverter)


def test_a_signal_carries_exactly_one_opaque_event_reference():
    assert ControlSignal(event_ref=EVENT_REF).event_ref == EVENT_REF
    with pytest.raises(ControlContractError):
        ControlSignal(event_ref="review resolved by Wanjiru Kamau")


def test_a_heartbeat_carries_stage_and_attempt_integers_only():
    beat = ControlHeartbeat(step_id="ingest", attempt_no=2)
    assert beat.as_control_mapping() == {"step_id": "ingest", "attempt_no": 2}


# --- section 12: failure classification ---------------------------------------


def test_the_failure_type_set_is_closed_and_non_retryable_types_are_marked():
    assert len(TEMPORAL_FAILURE_TYPES) == 9
    assert len(NON_RETRYABLE_FAILURE_TYPES) == 7
    assert NON_RETRYABLE_FAILURE_TYPES < TEMPORAL_FAILURE_TYPES


def test_a_sanitised_failure_carries_a_classification_and_control_details_only():
    error = sanitised_application_error("uncertain_write", details={"run_ref": RUN_REF})
    assert error.type == "uncertain_write"
    assert error.non_retryable is True
    assert error.message == "uncertain_write"
    retryable = sanitised_application_error("activity_internal")
    assert retryable.non_retryable is False


def test_an_undeclared_failure_type_or_a_pii_detail_is_refused():
    with pytest.raises(ControlContractError):
        sanitised_application_error("kaboom")
    with pytest.raises(ControlContractError):
        sanitised_application_error(
            "activity_internal", details={"insured_name": PRIVACY_SENTINELS["insured_name"]}
        )


# --- section 9: Workflow identity ----------------------------------------------


def test_every_workflow_id_builder_produces_the_exact_declared_form():
    assert agent_workflow_ref(RUN_REF).workflow_ref == f"pacha.agent.{RUN_REF}"
    assert chase_workflow_ref(CHECKLIST_REF).workflow_ref == f"pacha.chase.{CHECKLIST_REF}"
    assert docintel_workflow_ref(DOCUMENT_REF).workflow_ref == f"pacha.docintel.{DOCUMENT_REF}"
    assert intake_workflow_ref(TRIGGER_EVENT_REF).workflow_ref == (
        f"pacha.intake.{TRIGGER_EVENT_REF}"
    )
    assert assessment_workflow_ref(RUN_REF).workflow_ref == f"pacha.assessment.{RUN_REF}"
    assert approval_pack_workflow_ref(RUN_REF).workflow_ref == f"pacha.approval-pack.{RUN_REF}"
    assert projection_workflow_ref(PROJECTION_REF).workflow_ref == (
        f"pacha.projection.{PROJECTION_REF}"
    )


def test_a_workflow_ref_exposes_its_kind_and_subject_without_reparsing_by_hand():
    ref = chase_workflow_ref(CHECKLIST_REF)
    assert (ref.kind, ref.subject_ref) == ("chase", CHECKLIST_REF)
    assert str(ref) == f"pacha.chase.{CHECKLIST_REF}"
    assert parse_workflow_ref(str(ref)) == ("chase", CHECKLIST_REF)


@pytest.mark.parametrize(
    "value",
    [
        "Wanjiru Kamau",
        "MOT/2026/0099471",
        "KDA 812X",
        "1753300000000",
        RUN_REF.lower(),
        RUN_REF[:-1],
        "",
    ],
)
def test_id_builders_reject_anything_that_is_not_a_pacha_ulid(value):
    with pytest.raises(WorkflowIdError):
        agent_workflow_ref(value)


def test_an_id_builder_never_mints_an_identifier():
    """Two calls with the same ULID give the same ID; retries reuse it."""

    assert chase_workflow_ref(CHECKLIST_REF) == chase_workflow_ref(CHECKLIST_REF)


def test_a_workflow_ref_cannot_be_constructed_from_an_undeclared_form():
    with pytest.raises(ControlContractError):
        WorkflowRef(f"pacha.reaper.{RUN_REF}")
    with pytest.raises(WorkflowIdError):
        parse_workflow_ref(f"pacha.reaper.{RUN_REF}")


# --- section 12: retry policies -------------------------------------------------


def test_the_pack_policies_match_the_section_twelve_table_exactly():
    policies = load_retry_policies()
    assert set(policies) == set(POLICY_CEILINGS)

    db_control = policies["db_control"]
    assert db_control.retry_policy.initial_interval == parse_duration("x", "1s")
    assert db_control.retry_policy.backoff_coefficient == 2.0
    assert db_control.retry_policy.maximum_interval == parse_duration("x", "30s")
    assert db_control.retry_policy.maximum_attempts == 5
    assert db_control.start_to_close_timeout == parse_duration("x", "60s")
    assert db_control.heartbeat_timeout is None

    long_compute = policies["long_compute"]
    assert long_compute.retry_policy.maximum_attempts == 3
    assert long_compute.start_to_close_timeout == parse_duration("x", "2h")
    assert long_compute.heartbeat_timeout == parse_duration("x", "30s")

    ledger = policies["ledger_append"]
    assert ledger.retry_policy.maximum_interval == parse_duration("x", "10s")


@pytest.mark.parametrize("name", ["governed_external_write", "provider_managed_retry"])
def test_single_attempt_policies_have_exactly_one_temporal_attempt(name):
    policy = load_retry_policies()[name]
    assert policy.retry_policy.maximum_attempts == 1
    assert POLICY_CEILINGS[name].single_attempt is True


def _write_policies(tmp_path: pathlib.Path, mutate) -> pathlib.Path:
    document = yaml.safe_load(
        (_REPO / "packs" / "motor" / "orchestration.yaml").read_text(encoding="utf-8")
    )
    mutate(document)
    path = tmp_path / "orchestration.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("name", "key", "widened"),
    [
        ("db_control", "initial_interval", "2s"),
        ("db_control", "backoff_coefficient", 3.0),
        ("db_control", "maximum_interval", "60s"),
        ("db_control", "maximum_attempts", 6),
        ("db_control", "start_to_close_timeout", "61s"),
        ("long_compute", "maximum_attempts", 4),
        ("long_compute", "start_to_close_timeout", "3h"),
        ("long_compute", "heartbeat_timeout", "31s"),
        ("ledger_append", "maximum_interval", "11s"),
        ("ledger_append", "maximum_attempts", 6),
        ("governed_external_write", "maximum_attempts", 2),
        ("governed_external_write", "start_to_close_timeout", "3m"),
        ("provider_managed_retry", "maximum_attempts", 2),
        ("provider_managed_retry", "start_to_close_timeout", "11m"),
    ],
)
def test_pack_data_may_never_widen_a_hard_ceiling(tmp_path, name, key, widened):
    path = _write_policies(
        tmp_path, lambda doc: doc["retry_policies"][name].__setitem__(key, widened)
    )
    with pytest.raises(RetryPolicyError, match="exceeds|exactly 1"):
        load_retry_policies(path)


def test_pack_data_may_tighten_a_ceiling(tmp_path):
    path = _write_policies(
        tmp_path, lambda doc: doc["retry_policies"]["db_control"].__setitem__("maximum_attempts", 2)
    )
    assert load_retry_policies(path)["db_control"].retry_policy.maximum_attempts == 2


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda doc: doc.__setitem__("sampling_rate", 10), "unknown top-level"),
        (lambda doc: doc.pop("version"), "missing top-level"),
        (lambda doc: doc.__setitem__("version", 2), "version 1"),
        (lambda doc: doc["retry_policies"].__setitem__("reaper", {}), "exactly the section 12 set"),
        (lambda doc: doc["retry_policies"].pop("ledger_append"), "exactly the section 12 set"),
        (
            lambda doc: doc["retry_policies"]["db_control"].__setitem__("jitter", "1s"),
            "unknown keys",
        ),
        (lambda doc: doc["retry_policies"]["db_control"].pop("maximum_interval"), "missing keys"),
        (
            lambda doc: doc["retry_policies"]["db_control"].__setitem__("initial_interval", "1"),
            "duration",
        ),
        (
            lambda doc: doc["retry_policies"]["db_control"].__setitem__("initial_interval", "0s"),
            "positive duration",
        ),
        (
            lambda doc: doc["retry_policies"]["db_control"].__setitem__("maximum_attempts", 0),
            "positive integer",
        ),
        (
            lambda doc: doc["retry_policies"]["db_control"].__setitem__("backoff_coefficient", 0.5),
            "at least 1.0",
        ),
    ],
)
def test_the_policy_file_is_validated_strictly(tmp_path, mutate, match):
    with pytest.raises(RetryPolicyError, match=match):
        load_retry_policies(_write_policies(tmp_path, mutate))


def test_a_missing_policy_file_is_an_error_not_an_empty_policy_set(tmp_path):
    with pytest.raises(RetryPolicyError, match="unreadable"):
        load_retry_policies(tmp_path / "absent.yaml")


# --- sections 7 and 8: Worker roles ---------------------------------------------


def test_every_role_has_the_declared_task_queue_and_concurrency():
    config = local_config()
    assert set(ROLE_ACTIVITY_CONCURRENCY) == set(WORKER_ROLES)
    assert ROLE_ACTIVITY_CONCURRENCY == {
        "control": 20,
        "docintel": 4,
        "effects": 5,
        "ledger": 1,
    }
    assert [config.task_queue(role) for role in WORKER_ROLES] == [
        "pacha-test-control-v1",
        "pacha-test-docintel-v1",
        "pacha-test-effects-v1",
        "pacha-test-ledger-v1",
    ]
    assert WORKER_GRACEFUL_SHUTDOWN.total_seconds() == 60


def test_the_worker_factory_refuses_a_worker_with_no_explicit_registrations():
    with pytest.raises(ConfigurationError, match="does not discover"):
        build_worker(object(), local_config(), role="control")


def test_the_worker_factory_refuses_an_unknown_or_missing_role():
    with pytest.raises(ConfigurationError, match="PACHA_WORKER_ROLE"):
        build_worker(object(), local_config(), role="reaper", activities=[lambda: None])
    with pytest.raises(ConfigurationError, match="PACHA_WORKER_ROLE"):
        build_worker(object(), local_config(), activities=[lambda: None])


def test_the_worker_factory_refuses_to_let_a_caller_unpin_a_worker():
    for option in ("task_queue", "deployment_config", "graceful_shutdown_timeout"):
        with pytest.raises(ConfigurationError, match="fixed by role"):
            build_worker(
                object(),
                local_config(),
                role="control",
                activities=[lambda: None],
                **{option: "anything"},
            )


def test_pinned_versioning_is_the_declared_default_behaviour():
    assert VersioningBehavior.PINNED.name == "PINNED"


# --- section 23 regression: the Workflow sandbox pass-through --------------------


def test_the_sandbox_passes_through_only_deterministic_modules():
    """Regression: the whole `orchestration` tree was passed through, which put
    configuration, clients, KMS code and `os.urandom` inside replay context."""

    assert set(WORKFLOW_SAFE_MODULES) == {
        "orchestration.contracts",
        "orchestration.errors",
        "orchestration.ids",
        "orchestration.policies",
    }
    for forbidden in (
        "orchestration",
        "orchestration.client",
        "orchestration.codec",
        "orchestration.config",
        "orchestration.worker",
    ):
        assert forbidden not in WORKFLOW_SAFE_MODULES


def test_importing_the_package_pulls_in_no_client_configuration_or_codec():
    """The narrow pass-through list only holds if `__init__` stays lazy: an
    eager one would drag the client and Codec back in behind the four modules."""

    probe = (
        "import sys; import orchestration; "
        "leaked = [m for m in ('orchestration.client', 'orchestration.codec', "
        "'orchestration.config', 'orchestration.worker') if m in sys.modules]; "
        "assert not leaked, leaked; "
        "import orchestration.contracts; "
        "assert 'orchestration.codec' not in sys.modules; "
        "assert orchestration.build_worker.__name__ == 'build_worker'; "
        "assert 'orchestration.worker' in sys.modules"
    )
    environment = dict(os.environ, PYTHONPATH=str(_REPO / "platform"))
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


# --- section 22 regression: Temporal integration tests must not skip -------------


def test_the_temporal_integration_suite_contains_no_skip_guard():
    """Regression: a startup `except` turned every integration test into a skip,
    so CI could report green with a broken Worker or SDK configuration."""

    source = (_REPO / "tests" / "integration" / "test_temporal_orchestration.py").read_text(
        encoding="utf-8"
    )
    offenders = [
        marker
        for marker in ("pytest.skip", "pytest.mark.skip", "skipif", "importorskip")
        if marker in source
    ]
    assert offenders == []


# --- history privacy -------------------------------------------------------------


def test_sentinel_scanning_finds_seeded_values_case_insensitively():
    blob = b"prefix wanjiru KAMAU suffix"
    assert find_sentinels(blob, ["Wanjiru Kamau"]) == ["Wanjiru Kamau"]
    assert find_sentinels(blob, ["KDA 812X"]) == []
    with pytest.raises(HistoryPrivacyError, match="1 forbidden sentinel"):
        assert_no_sentinels(blob, PRIVACY_SENTINELS.values(), source="fixture")


def test_a_clean_blob_passes_the_privacy_assertion():
    assert_no_sentinels(f"pacha.chase.{CHECKLIST_REF}", PRIVACY_SENTINELS.values(), source="id")


def test_scanning_something_that_is_not_a_history_is_an_error():
    from orchestration.history import history_blob

    with pytest.raises(HistoryPrivacyError, match="no history events"):
        history_blob(object())


# --- repository invariants --------------------------------------------------------


def test_no_production_package_imports_from_the_isolated_spike():
    """Master plan section 5: the spike is historical evidence, not a dependency."""

    pattern = re.compile(r"^\s*(?:from|import)\s+spikes\b", re.MULTILINE)
    offenders = [
        str(path.relative_to(_REPO))
        for root in ("platform", "agents", "packs", "console", "tools")
        for path in (_REPO / root).rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_the_public_interface_exposes_only_the_declared_names():
    """The closed export set through T07's schedule bootstrap."""

    import orchestration
    from orchestration.schedules import bootstrap_schedules

    assert sorted(orchestration.__all__) == [
        "ControlResult",
        "TemporalConfig",
        "TemporalStarter",
        "WorkflowRef",
        "bootstrap_schedules",
        "build_data_converter",
        "build_temporal_client",
        "build_worker",
    ]
    assert orchestration.bootstrap_schedules is bootstrap_schedules
    # T02's other new objects are wired explicitly at the Worker call site and
    # must not become package-root exports.
    for unexported in (
        "TemporalIntentConsumer",
        "SystemActivities",
        "AgentRunActivities",
        "SYSTEM_WORKFLOWS",
    ):
        assert not hasattr(orchestration, unexported)

    # Lazily resolved, but resolved to the real objects.
    assert orchestration.TemporalConfig is TemporalConfig
    assert orchestration.build_worker is build_worker
    assert dir(orchestration) == sorted(orchestration.__all__)
