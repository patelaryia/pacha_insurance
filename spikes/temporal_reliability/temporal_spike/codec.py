"""AES-GCM Payload Codec used as defence in depth for control-only history."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import replace

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from temporalio.api.common.v1 import Payload
from temporalio.converter import DataConverter, PayloadCodec

ENCODING = b"binary/encrypted"
KEY_ID = b"spike-ephemeral-key"


class AesGcmPayloadCodec(PayloadCodec):
    """Encrypt complete serialized Payload messages with an ephemeral test key."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("AES-256-GCM requires a 32-byte key")
        self._cipher = AESGCM(key)

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        encoded: list[Payload] = []
        for payload in payloads:
            nonce = os.urandom(12)
            ciphertext = self._cipher.encrypt(
                nonce,
                payload.SerializeToString(),
                KEY_ID,
            )
            encoded.append(
                Payload(
                    metadata={
                        "encoding": ENCODING,
                        "encryption-key-id": KEY_ID,
                    },
                    data=nonce + ciphertext,
                )
            )
        return encoded

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        decoded: list[Payload] = []
        for payload in payloads:
            if payload.metadata.get("encoding") != ENCODING:
                decoded.append(payload)
                continue
            raw = self._cipher.decrypt(
                payload.data[:12],
                payload.data[12:],
                payload.metadata["encryption-key-id"],
            )
            original = Payload()
            original.ParseFromString(raw)
            decoded.append(original)
        return decoded


def encrypted_data_converter(key: bytes) -> DataConverter:
    return replace(DataConverter.default, payload_codec=AesGcmPayloadCodec(key))
