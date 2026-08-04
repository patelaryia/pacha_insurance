"""T09 infrastructure and executable-runtime contract."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from orchestration.config import TemporalConfig
from orchestration.errors import ConfigurationError
from orchestration.observability import CONTROL_LOG_FIELDS, ControlJsonFormatter
from orchestration.runtime import _load_factory
from orchestration.telemetry import build_runtime_telemetry

REPO = Path(__file__).resolve().parents[2]
MODULE = REPO / "infra/terraform/modules/temporal_worker"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _cloud_config(*, role: str = "control") -> TemporalConfig:
    return TemporalConfig(
        env="staging",
        mode="cloud",
        address="pacha-staging.tmprl.cloud:7233",
        namespace="pacha-staging.ab12c",
        queue_prefix="pacha-staging",
        build_id="a" * 40,
        region="af-south-1",
        tls_cert_secret_arn=(
            "arn:aws:secretsmanager:af-south-1:123456789012:secret:temporal-cert"
        ),
        tls_key_secret_arn=(
            "arn:aws:secretsmanager:af-south-1:123456789012:secret:temporal-key"
        ),
        kms_key_arn=(
            "arn:aws:kms:af-south-1:123456789012:key/"
            "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
        ),
        worker_role=role,
    )


def test_t09_creates_both_environments_and_the_worker_module():
    expected = {
        REPO / f"infra/terraform/environments/{environment}/{name}"
        for environment in ("staging", "prod")
        for name in ("main.tf", "variables.tf", "versions.tf", "outputs.tf")
    }
    expected |= {
        MODULE / name
        for name in ("main.tf", "variables.tf", "versions.tf", "outputs.tf")
    }
    assert all(path.is_file() for path in expected)


def test_t09_worker_topology_is_exact_and_has_no_inbound_surface():
    source = _text(MODULE / "main.tf")
    for role, count, concurrency in (
        ("control", 2, 20),
        ("docintel", 2, 4),
        ("effects", 1, 5),
        ("ledger", 1, 1),
    ):
        block = source.split(f"{role} = {{", 1)[1].split("}", 1)[0]
        assert f"desired_count = {count}" in block
        assert f"concurrency   = {concurrency}" in block
    assert "aws_vpc_security_group_ingress_rule" not in source
    assert "load_balancer" not in source
    assert "assign_public_ip = false" in source
    assert "portMappings = []" in source
    assert 'stopTimeout = 120' in source
    assert '["python", "-m", "orchestration.runtime"]' in source


def test_t09_refuses_world_open_or_mutable_deployments():
    variables = _text(MODULE / "variables.tf")
    source = _text(MODULE / "main.tf")
    assert 'contains(var.temporal_cloud_cidr_blocks, "0.0.0.0/0")' in variables
    assert 'contains(var.provider_https_cidr_blocks, "0.0.0.0/0")' in variables
    assert '@sha256:[0-9a-f]{64}$' in variables
    assert 'can(regex("^[0-9a-f]{40}$", var.build_id))' in variables
    assert 'cidr_ipv4         = "0.0.0.0/0"' not in source
    assert "enable_execute_command             = false" in source


def test_t09_task_roles_are_scoped_and_mtls_is_never_an_ecs_plaintext_secret():
    source = _text(MODULE / "main.tf")
    for action in (
        "secretsmanager:GetSecretValue",
        "kms:Decrypt",
        "kms:GenerateDataKey",
        "rds-db:connect",
        "s3:GetObject",
        "s3:PutObject",
        "s3:ListBucket",
    ):
        assert action in source
    assert "temporal_tls_cert_secret_arn" in source
    assert "temporal_tls_key_secret_arn" in source
    assert "client_cert" not in source
    assert "client_private_key" not in source


def test_t09_provisions_every_binding_alert_threshold():
    source = _text(MODULE / "main.tf")
    expected_metrics = {
        "OutboxOldestAgeSeconds": "300",
        "LedgerOldestAgeSeconds": "60",
        "LedgerHashFailureCount": "0",
        "UncertainWriteCount": "0",
        "ControlScheduleToStartP95Seconds": "30",
        "ControlSecondsSinceLastPoll": "120",
        "CodecKmsFailureCount": "0",
        "ScheduleActionFailureCount": "0",
        "WorkflowFailureRatePercent": "1",
    }
    for metric, threshold in expected_metrics.items():
        metric_block = source.split(f'metric      = "{metric}"', 1)[1].split("}", 1)[0]
        assert f"threshold   = {threshold}" in metric_block
    assert 'namespace           = "Pacha/Temporal"' in source
    assert 'treat_missing_data  = "breaching"' in source


def test_t09_sdk_telemetry_is_loopback_only_and_labelled():
    config = _cloud_config(role="effects")
    runtime = build_runtime_telemetry(config)
    source = _text(REPO / "platform/orchestration/telemetry.py")
    assert runtime is not None
    assert "127.0.0.1:4317" in source
    assert '"environment": config.env' in source
    assert '"build_id": config.build_id' in source
    assert '"worker_role": config.worker_role' in source


def test_t09_telemetry_refuses_non_cloud_and_dependency_factory_is_explicit():
    local = TemporalConfig(
        env="test",
        mode="local",
        address="localhost:7233",
        namespace="pacha-test",
        queue_prefix="pacha-test",
        build_id="b" * 40,
        worker_role="control",
    )
    with pytest.raises(ConfigurationError, match="staging/prod cloud mode"):
        build_runtime_telemetry(local)
    with pytest.raises(ConfigurationError, match="module:attribute"):
        _load_factory("not-a-reference")


def test_t09_registration_is_closed_in_code_not_returned_by_the_factory():
    source = _text(REPO / "platform/orchestration/runtime.py")
    assert "class WorkerDependencies" in source
    assert "workflows:" not in source.split("class WorkerDependencies", 1)[1].split(
        "class DependenciesFactory", 1
    )[0]
    assert "activities:" not in source.split("class WorkerDependencies", 1)[1].split(
        "class DependenciesFactory", 1
    )[0]
    for registration in (
        "SYSTEM_WORKFLOWS",
        "RECURRING_WORKFLOWS",
        "DocumentChaseWorkflow",
        "INTAKE_WORKFLOWS",
        "ASSESSMENT_WORKFLOWS",
        "APPROVAL_PACK_WORKFLOWS",
        "PROJECTION_WORKFLOWS",
        "ledger_activity_registrations",
        "docintel_activity_registrations",
    ):
        assert registration in source


def test_t09_log_formatter_drops_messages_arguments_tracebacks_and_unknown_fields():
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="Jane Doe policy KZZ-998 bank 001234",
        args=(),
        exc_info=(ValueError, ValueError("secret claim fact"), None),
    )
    record.run_ref = "01H00000000000000000000001"
    record.unreviewed = "must-not-appear"
    output = ControlJsonFormatter(_cloud_config()).format(record)
    payload = json.loads(output)
    assert set(payload) <= set(CONTROL_LOG_FIELDS)
    assert payload["run_ref"] == "01H00000000000000000000001"
    assert payload["error_code"] == "unclassified_exception"
    for forbidden in ("Jane", "KZZ-998", "001234", "secret", "must-not-appear"):
        assert forbidden not in output
