"""AES-256-GCM Payload Codec with KMS envelope encryption (master plan §11).

Encryption here is defence in depth and nothing more. The control-only contract
in `contracts` still decides what may be placed in a payload; this module only
ensures that what does go in is unreadable at rest in Temporal's history.

The mechanics the plan fixes, and why each one matters:

* one `GenerateDataKey` per `encode` batch — bounded KMS cost without ever
  caching a plaintext data key across calls;
* a unique 12-byte nonce per Payload — two identical payloads must never
  produce identical ciphertext;
* the complete serialized `Payload` is the plaintext, so metadata Temporal
  would otherwise see in clear is encrypted too;
* AAD binds every ciphertext to the namespace and the KMS key, so a payload
  cannot be replayed into a different namespace;
* decode groups by wrapped data key and calls `Decrypt` once per group.

There is no plaintext path. An unknown Codec version, an unapproved key ARN, a
malformed nonce, a KMS failure or a failed authentication tag is a refusal, not
a downgrade.

**Key identity is an immutable key ARN, never an alias.** `GenerateDataKey`
returns the canonical key ARN in `KeyId` however the key was addressed, so a
Codec whose allowlist is pinned to a configured alias rejects its own freshly
generated data key and encoding fails in production. Rotation therefore uses KMS
rotation of the key *material*, which preserves the ARN and retains the previous
material, so history encrypted before a rotation stays decryptable with no
configuration change. `config` refuses an alias ARN outright.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol, runtime_checkable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from temporalio.api.common.v1 import Payload
from temporalio.converter import DataConverter, PayloadCodec

from orchestration.config import TemporalConfig
from orchestration.contracts import ControlPayloadConverter
from orchestration.errors import CodecError

__all__ = [
    "CODEC_ENCODING",
    "CODEC_VERSION",
    "DataKeyProvider",
    "GeneratedDataKey",
    "KmsDataKeyProvider",
    "PachaPayloadCodec",
    "StaticDataKeyProvider",
    "build_data_converter",
]

CODEC_VERSION = b"1"
CODEC_ENCODING = b"binary/pacha-aesgcm-v1"

_METADATA_ENCODING = "encoding"
_METADATA_VERSION = "pacha-codec-version"
_METADATA_KEY_ARN = "pacha-kms-key-arn"
_METADATA_WRAPPED_KEY = "pacha-wrapped-key"
_METADATA_NONCE = "pacha-nonce"

_NONCE_BYTES = 12
_DATA_KEY_BYTES = 32

#: Section 11 — boto3 is synchronous, so KMS runs on threads behind a
#: process-local bound of eight concurrent calls.
_KMS_CONCURRENCY = 8
_KMS_SEMAPHORE = asyncio.Semaphore(_KMS_CONCURRENCY)

_KMS_CONNECT_TIMEOUT_SECONDS = 2
_KMS_READ_TIMEOUT_SECONDS = 5
_KMS_MAX_ATTEMPTS = 3


def _aad(namespace: str, key_arn: str) -> bytes:
    """The section 11 additional authenticated data, verbatim."""

    return f"pacha-temporal-codec-v1|{namespace}|{key_arn}".encode()


@dataclass(frozen=True, slots=True)
class GeneratedDataKey:
    """One envelope data key: plaintext for this batch, wrapped for storage."""

    key_arn: str
    plaintext: bytes
    wrapped: bytes


@runtime_checkable
class DataKeyProvider(Protocol):
    """The envelope-key seam. Production is KMS; tests inject a static key."""

    async def generate_data_key(self) -> GeneratedDataKey:
        """Return a fresh 32-byte data key and its wrapped form."""
        ...

    async def unwrap_data_key(self, key_arn: str, wrapped: bytes) -> bytes:
        """Recover the plaintext of a previously wrapped data key."""
        ...


class KmsDataKeyProvider:
    """AWS KMS envelope keys with the section 11 timeout and retry posture."""

    def __init__(self, key_arn: str, *, client: Any | None = None) -> None:
        self._key_arn = key_arn
        self._client = client if client is not None else self._build_client()

    @staticmethod
    def _build_client() -> Any:
        import boto3
        from botocore.config import Config

        return boto3.client(
            "kms",
            config=Config(
                connect_timeout=_KMS_CONNECT_TIMEOUT_SECONDS,
                read_timeout=_KMS_READ_TIMEOUT_SECONDS,
                # `max_attempts` in a botocore Config counts retries only.
                # `total_max_attempts` includes the initial call, matching the
                # master plan's hard ceiling of three total attempts.
                retries={"mode": "standard", "total_max_attempts": _KMS_MAX_ATTEMPTS},
            ),
        )

    async def generate_data_key(self) -> GeneratedDataKey:
        async with _KMS_SEMAPHORE:
            try:
                response = await asyncio.to_thread(
                    self._client.generate_data_key,
                    KeyId=self._key_arn,
                    KeySpec="AES_256",
                )
            except Exception as error:  # noqa: BLE001 - any KMS failure is a refusal
                raise CodecError("KMS GenerateDataKey failed; refusing to encode") from error
        plaintext = response["Plaintext"]
        if len(plaintext) != _DATA_KEY_BYTES:
            raise CodecError("KMS returned a data key that is not 256 bits")
        return GeneratedDataKey(
            key_arn=response.get("KeyId", self._key_arn),
            plaintext=plaintext,
            wrapped=response["CiphertextBlob"],
        )

    async def unwrap_data_key(self, key_arn: str, wrapped: bytes) -> bytes:
        async with _KMS_SEMAPHORE:
            try:
                response = await asyncio.to_thread(
                    self._client.decrypt,
                    KeyId=key_arn,
                    CiphertextBlob=wrapped,
                )
            except Exception as error:  # noqa: BLE001 - any KMS failure is a refusal
                raise CodecError("KMS Decrypt failed; refusing to decode") from error
        plaintext = response["Plaintext"]
        if len(plaintext) != _DATA_KEY_BYTES:
            raise CodecError("KMS returned a data key that is not 256 bits")
        return plaintext


class StaticDataKeyProvider:
    """A fixed synthetic data key. Tests only — refused in staging and prod."""

    def __init__(self, key: bytes, *, key_arn: str = "static/pacha-test-codec-key") -> None:
        if len(key) != _DATA_KEY_BYTES:
            raise CodecError("a static Codec key must be exactly 32 bytes")
        self._key = bytes(key)
        self._key_arn = key_arn

    @property
    def key_arn(self) -> str:
        return self._key_arn

    async def generate_data_key(self) -> GeneratedDataKey:
        return GeneratedDataKey(key_arn=self._key_arn, plaintext=self._key, wrapped=b"static")

    async def unwrap_data_key(self, key_arn: str, wrapped: bytes) -> bytes:
        if key_arn != self._key_arn or wrapped != b"static":
            raise CodecError("static Codec provider cannot unwrap this data key")
        return self._key


def _zero(buffer: bytearray) -> None:
    """Overwrite a plaintext key buffer as far as CPython permits."""

    for index in range(len(buffer)):
        buffer[index] = 0


class PachaPayloadCodec(PayloadCodec):
    """The only Codec Pacha registers on a Temporal client or Worker."""

    def __init__(
        self,
        provider: DataKeyProvider,
        *,
        namespace: str,
        approved_key_arns: Iterable[str],
    ) -> None:
        approved = frozenset(approved_key_arns)
        if not approved:
            raise CodecError("the Codec requires at least one approved KMS key ARN")
        self._provider = provider
        self._namespace = namespace
        self._approved_key_arns = approved

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        if not payloads:
            return []

        data_key = await self._provider.generate_data_key()
        if data_key.key_arn not in self._approved_key_arns:
            raise CodecError("data key ARN is outside the approved Codec allowlist")
        if len(data_key.plaintext) != _DATA_KEY_BYTES:
            raise CodecError("envelope data key is not 256 bits")

        buffer = bytearray(data_key.plaintext)
        aad = _aad(self._namespace, data_key.key_arn)
        try:
            cipher = AESGCM(bytes(buffer))
            encoded: list[Payload] = []
            for payload in payloads:
                nonce = os.urandom(_NONCE_BYTES)
                ciphertext = cipher.encrypt(nonce, payload.SerializeToString(), aad)
                encoded.append(
                    Payload(
                        metadata={
                            _METADATA_ENCODING: CODEC_ENCODING,
                            _METADATA_VERSION: CODEC_VERSION,
                            _METADATA_KEY_ARN: data_key.key_arn.encode("utf-8"),
                            _METADATA_WRAPPED_KEY: data_key.wrapped,
                            _METADATA_NONCE: nonce,
                        },
                        data=ciphertext,
                    )
                )
            return encoded
        finally:
            _zero(buffer)
            del buffer

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        if not payloads:
            return []

        groups: dict[tuple[str, bytes], list[int]] = {}
        for index, payload in enumerate(payloads):
            key_arn, wrapped = self._inspect(payload)
            groups.setdefault((key_arn, wrapped), []).append(index)

        decoded: list[Payload | None] = [None] * len(payloads)
        for (key_arn, wrapped), indexes in groups.items():
            plaintext_key = await self._provider.unwrap_data_key(key_arn, wrapped)
            if len(plaintext_key) != _DATA_KEY_BYTES:
                raise CodecError("unwrapped data key is not 256 bits")
            buffer = bytearray(plaintext_key)
            aad = _aad(self._namespace, key_arn)
            try:
                cipher = AESGCM(bytes(buffer))
                for index in indexes:
                    payload = payloads[index]
                    nonce = payload.metadata[_METADATA_NONCE]
                    try:
                        plaintext = cipher.decrypt(nonce, payload.data, aad)
                    except InvalidTag as error:
                        raise CodecError(
                            "Payload failed authentication; ciphertext, nonce, AAD "
                            "or metadata was altered"
                        ) from error
                    original = Payload()
                    try:
                        original.ParseFromString(plaintext)
                    except Exception as error:  # noqa: BLE001 - malformed inner payload
                        raise CodecError("decrypted bytes are not a Temporal Payload") from error
                    decoded[index] = original
            finally:
                _zero(buffer)
                del buffer

        return [payload for payload in decoded if payload is not None]

    def _inspect(self, payload: Payload) -> tuple[str, bytes]:
        """Validate one encoded Payload's metadata, refusing anything else."""

        metadata = payload.metadata
        if metadata.get(_METADATA_ENCODING) != CODEC_ENCODING:
            raise CodecError("payload is not Pacha-encrypted; plaintext is never accepted")
        if metadata.get(_METADATA_VERSION) != CODEC_VERSION:
            raise CodecError("unknown Pacha Codec version")
        raw_arn = metadata.get(_METADATA_KEY_ARN)
        wrapped = metadata.get(_METADATA_WRAPPED_KEY)
        nonce = metadata.get(_METADATA_NONCE)
        if raw_arn is None or wrapped is None or nonce is None:
            raise CodecError("encrypted payload is missing required Codec metadata")
        if len(nonce) != _NONCE_BYTES:
            raise CodecError("encrypted payload has a malformed nonce")
        try:
            key_arn = raw_arn.decode("utf-8")
        except UnicodeDecodeError as error:
            raise CodecError("encrypted payload has a malformed key ARN") from error
        if key_arn not in self._approved_key_arns:
            raise CodecError("payload key ARN is outside the approved Codec allowlist")
        return key_arn, wrapped


def build_data_converter(
    config: TemporalConfig,
    *,
    data_key_provider: DataKeyProvider | None = None,
    approved_key_arns: Iterable[str] | None = None,
) -> DataConverter:
    """The encrypted Data Converter every Pacha Temporal client must use.

    Production constructs `KmsDataKeyProvider` from `PACHA_TEMPORAL_KMS_KEY_ARN`.
    A provider may be injected in `dev` and `test` only — *any* injected
    provider is refused in `staging` and `prod`, not merely the static synthetic
    one, since a custom provider can return arbitrary key material. There is no
    unencrypted converter to fall back to: a configuration with neither a KMS key
    nor an injected provider raises.

    Raises:
        CodecError: a provider was injected in a production-like environment, or
            no envelope key source is available at all.
    """

    if config.is_production_like and approved_key_arns is not None:
        raise CodecError(
            f"an injected Codec key allowlist is refused in {config.env}; "
            "production uses only PACHA_TEMPORAL_KMS_KEY_ARN"
        )

    if data_key_provider is None:
        if config.kms_key_arn is None:
            raise CodecError(
                "no Codec key source: set PACHA_TEMPORAL_KMS_KEY_ARN or inject a test provider"
            )
        provider: DataKeyProvider = KmsDataKeyProvider(config.kms_key_arn)
        approved = approved_key_arns or (config.kms_key_arn,)
    else:
        # Every injected provider is a test seam, not only the static one: a
        # custom provider could return any key material at all, so type is not
        # the discriminator — the environment is.
        if config.is_production_like:
            raise CodecError(
                f"an injected Codec data-key provider is refused in {config.env}; "
                "production constructs the KMS provider itself"
            )
        provider = data_key_provider
        if approved_key_arns is not None:
            approved = approved_key_arns
        elif isinstance(data_key_provider, StaticDataKeyProvider):
            approved = (data_key_provider.key_arn,)
        elif config.kms_key_arn is not None:
            approved = (config.kms_key_arn,)
        else:
            raise CodecError("an injected Codec provider requires an approved key ARN allowlist")

    return replace(
        DataConverter.default,
        payload_converter_class=ControlPayloadConverter,
        payload_codec=PachaPayloadCodec(
            provider,
            namespace=config.namespace,
            approved_key_arns=approved,
        ),
    )
