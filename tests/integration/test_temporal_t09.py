"""T09 keeps Cloud configuration fail-closed before a Worker polls."""

from __future__ import annotations

import pytest

from orchestration.config import TemporalConfig
from orchestration.errors import ConfigurationError
from orchestration.runtime import _dependencies


def test_t09_cloud_worker_refuses_to_build_without_the_code_owned_factory(monkeypatch):
    monkeypatch.delenv("PACHA_WORKER_DEPENDENCIES_FACTORY", raising=False)
    config = TemporalConfig(
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
        worker_role="control",
    )
    with pytest.raises(ConfigurationError, match="PACHA_WORKER_DEPENDENCIES_FACTORY"):
        _dependencies(config)
