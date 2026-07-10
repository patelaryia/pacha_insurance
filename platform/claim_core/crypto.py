"""ED-6a envelope encryption and blind-index mechanics."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
from typing import Any, Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class KeyProvider(Protocol):
    """Key-management boundary implemented by local keys and, later, KMS."""

    def generate_dek(self) -> bytes: ...

    def wrap(self, dek: bytes) -> bytes: ...

    def unwrap(self, wrapped: bytes) -> bytes: ...

    def index_hmac(self, normalised: str) -> str: ...


def _env_key(name: str) -> bytes:
    encoded = os.environ.get(name)
    if encoded is None:
        return secrets.token_bytes(32)
    try:
        key = base64.b64decode(encoded, validate=True)
    except ValueError as error:
        raise ValueError(f"{name} must be valid base64") from error
    if len(key) != 32:
        raise ValueError(f"{name} must decode to exactly 32 bytes")
    return key


class LocalKeyProvider:
    """AES-GCM local provider for development and deterministic tests."""

    def __init__(self, master_key: bytes, index_key: bytes) -> None:
        if len(master_key) != 32 or len(index_key) != 32:
            raise ValueError("local encryption and index keys must be 32 bytes")
        self._master_key = master_key
        self._index_key = index_key

    @classmethod
    def from_environment(cls) -> LocalKeyProvider:
        return cls(
            _env_key("PACHA_LOCAL_MASTER_KEY"),
            _env_key("PACHA_LOCAL_INDEX_KEY"),
        )

    def generate_dek(self) -> bytes:
        return secrets.token_bytes(32)

    def wrap(self, dek: bytes) -> bytes:
        nonce = secrets.token_bytes(12)
        return nonce + AESGCM(self._master_key).encrypt(nonce, dek, None)

    def unwrap(self, wrapped: bytes) -> bytes:
        if len(wrapped) < 29:
            raise ValueError("wrapped DEK is malformed")
        return AESGCM(self._master_key).decrypt(wrapped[:12], wrapped[12:], None)

    def index_hmac(self, normalised: str) -> str:
        return hmac.new(
            self._index_key, normalised.encode("utf-8"), hashlib.sha256
        ).hexdigest()


def encrypt_value(value: Any, dek: bytes) -> dict[str, Any]:
    """Encrypt one JSON value as the binding AES-256-GCM envelope."""

    nonce = secrets.token_bytes(12)
    plaintext = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    ciphertext = AESGCM(dek).encrypt(nonce, plaintext, None)
    return {
        "__enc__": {
            "alg": "AES-256-GCM",
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ct": base64.b64encode(ciphertext).decode("ascii"),
        }
    }


def decrypt_value(value: Any, dek: bytes) -> Any:
    """Decrypt a value produced by :func:`encrypt_value`."""

    if not isinstance(value, dict) or set(value) != {"__enc__"}:
        raise ValueError("encrypted PII envelope is malformed")
    envelope = value["__enc__"]
    if envelope.get("alg") != "AES-256-GCM":
        raise ValueError("encrypted PII algorithm is unsupported")
    nonce = base64.b64decode(envelope["nonce"], validate=True)
    ciphertext = base64.b64decode(envelope["ct"], validate=True)
    plaintext = AESGCM(dek).decrypt(nonce, ciphertext, None)
    return json.loads(plaintext)


def normalise_blind_index(path: str, value: Any) -> str:
    """Apply Packet-03's registered equality-search normalisation."""

    text_value = str(value)
    if path == "parties.insured.phone":
        digits = re.sub(r"\D", "", text_value)
        if digits.startswith("0"):
            digits = f"254{digits[1:]}"
        return digits
    return re.sub(r"[\s-]", "", text_value).upper()
