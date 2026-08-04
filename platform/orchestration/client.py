"""Temporal client construction (master plan section 6).

One function builds every Temporal client Pacha owns, so the security posture
cannot vary by call site: the encrypted Data Converter, `HeaderCodecBehavior.CODEC`
and the control-payload interceptor are not optional arguments.

Cloud authentication is mTLS only. Certificate and key bytes are fetched once
from Secrets Manager at process start, held in process memory, and passed
straight into `TLSConfig`. They are never written to disk, never logged, and
never placed in an exception message — the failure paths below name the ARN
variable, not its contents.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from temporalio.client import Client, HeaderCodecBehavior, Interceptor, TLSConfig
from temporalio.runtime import Runtime

from orchestration.codec import DataKeyProvider, build_data_converter
from orchestration.config import TemporalConfig
from orchestration.contracts import ControlPayloadInterceptor
from orchestration.errors import ConfigurationError

__all__ = [
    "AwsSecretsManagerProvider",
    "SecretBytesProvider",
    "build_temporal_client",
]


@runtime_checkable
class SecretBytesProvider(Protocol):
    """The Secrets Manager seam. Injected only by tests (section 6)."""

    def secret_bytes(self, secret_arn: str) -> bytes:
        """Return the raw secret value for `secret_arn`."""
        ...


class AwsSecretsManagerProvider:
    """`secretsmanager:GetSecretValue`, in memory, once per process."""

    def __init__(self, *, client: Any | None = None) -> None:
        self._client = client if client is not None else self._build_client()

    @staticmethod
    def _build_client() -> Any:
        import boto3

        return boto3.client("secretsmanager")

    def secret_bytes(self, secret_arn: str) -> bytes:
        try:
            response = self._client.get_secret_value(SecretId=secret_arn)
        except Exception as error:  # noqa: BLE001 - never surface the raw AWS text
            raise ConfigurationError(
                "could not fetch Temporal mTLS material from Secrets Manager"
            ) from error
        if "SecretBinary" in response and response["SecretBinary"]:
            return bytes(response["SecretBinary"])
        secret = response.get("SecretString")
        if not secret:
            raise ConfigurationError("Temporal mTLS secret is empty")
        return secret.encode("utf-8")


def _build_tls_config(
    config: TemporalConfig,
    secret_provider: SecretBytesProvider | None,
) -> TLSConfig:
    if config.tls_cert_secret_arn is None or config.tls_key_secret_arn is None:
        raise ConfigurationError("cloud mode requires both mTLS Secrets Manager ARNs")
    provider = secret_provider if secret_provider is not None else AwsSecretsManagerProvider()
    client_cert = provider.secret_bytes(config.tls_cert_secret_arn)
    client_key = provider.secret_bytes(config.tls_key_secret_arn)
    if not client_cert or not client_key:
        raise ConfigurationError("Temporal mTLS certificate or private key is empty")
    return TLSConfig(client_cert=client_cert, client_private_key=client_key)


async def build_temporal_client(
    config: TemporalConfig,
    *,
    secret_provider: SecretBytesProvider | None = None,
    data_key_provider: DataKeyProvider | None = None,
    interceptors: Sequence[Interceptor] = (),
    runtime: Runtime | None = None,
) -> Client:
    """Connect to Temporal with Pacha's mandatory security configuration.

    Args:
        config: a validated section 6 configuration.
        secret_provider: test seam for the mTLS material; production builds the
            AWS Secrets Manager implementation itself.
        data_key_provider: test seam for the Codec envelope key; production
            builds the KMS implementation from the configured key ARN.
        interceptors: additional client interceptors, appended after the
            mandatory control-payload interceptor.

    Raises:
        ConfigurationError: a test seam was supplied in a production-like
            environment, or the mTLS material is unavailable.
    """

    # Test seams exist in `dev` and `test` and nowhere else. Both are refused by
    # environment rather than by type: a custom provider of either kind can
    # supply arbitrary credentials or key material.
    if config.is_production_like:
        injected = [
            name
            for name, seam in (
                ("secret_provider", secret_provider),
                ("data_key_provider", data_key_provider),
            )
            if seam is not None
        ]
        if injected:
            raise ConfigurationError(
                f"injected test seams are refused in {config.env}: {', '.join(injected)}"
            )

    data_converter = build_data_converter(config, data_key_provider=data_key_provider)
    tls: TLSConfig | bool | None = None
    if config.is_cloud:
        tls = _build_tls_config(config, secret_provider)

    return await Client.connect(
        config.address,
        namespace=config.namespace,
        tls=tls,
        data_converter=data_converter,
        # SDK interceptors execute in list order. Pacha's guard is deliberately
        # last so an earlier tracing/custom interceptor cannot add a header,
        # memo or free-text field after validation has already run.
        interceptors=[*interceptors, ControlPayloadInterceptor()],
        header_codec_behavior=HeaderCodecBehavior.CODEC,
        runtime=runtime,
    )
