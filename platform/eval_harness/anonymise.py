"""Fail-closed structured corpus anonymisation with claim-scoped HMAC tokens."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

PII_KINDS = frozenset({"name", "id", "phone"})
FREE_TEXT_PATH = re.compile(r"(?:narrative|remarks|notes?|free[_-]?text)", re.IGNORECASE)
PII_PATH = re.compile(r"(?:^|\.)(?:name|national_id|id_no|phone)(?:$|\.)", re.IGNORECASE)
FREE_TEXT_KEYS = frozenset(
    {
        "body",
        "content",
        "description",
        "message",
        "narrative",
        "note",
        "notes",
        "prose",
        "remarks",
        "text",
    }
)
SCALAR_VALUE_TYPES = frozenset({"string", "money", "date", "datetime", "bool", "enum"})


class AnonymisationRefused(ValueError):
    """The complete export was refused because one surface was unsafe."""


def _normalise(value: str) -> str:
    return " ".join(value.casefold().split())


def _digits(digest: bytes, length: int) -> str:
    stream = "".join(f"{byte:03d}" for byte in digest)
    return (stream * ((length // len(stream)) + 1))[:length]


def _pseudonym(kind: str, value: str, digest: bytes) -> str:
    if kind == "name":
        return f"Person {digest.hex()[:12].upper()}"
    digit_count = len(re.sub(r"\D", "", value))
    if digit_count == 0:
        raise AnonymisationRefused(f"{kind} value has no digits")
    token = _digits(digest, digit_count)
    if kind == "phone" and value.strip().startswith("+"):
        return f"+{token}"
    return token


def _reject_binary(value: Any) -> None:
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise AnonymisationRefused("binary input is not supported")
    if isinstance(value, dict):
        value_type = value.get("value_type")
        mime = value.get("mime")
        if value_type in {"binary", "bytes", "image"} or (
            isinstance(mime, str) and mime.casefold().startswith("image/")
        ):
            raise AnonymisationRefused("binary/image input is not supported")
        for child in value.values():
            _reject_binary(child)
    elif isinstance(value, list):
        for child in value:
            _reject_binary(child)


def _reject_ambiguous_free_text(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if (
                isinstance(key, str)
                and key.casefold() in FREE_TEXT_KEYS
                and isinstance(child, str)
                and child
            ):
                raise AnonymisationRefused("ambiguous free text is not exportable")
            _reject_ambiguous_free_text(child)
    elif isinstance(value, list):
        for child in value:
            _reject_ambiguous_free_text(child)


def anonymise_bundle(bundle: Any, *, claim_key: str, secret: bytes) -> dict[str, Any]:
    """Return an all-or-nothing anonymised copy of one structured claim bundle."""

    if not isinstance(bundle, dict):
        raise AnonymisationRefused("bundle must be a structured object")
    if not isinstance(claim_key, str) or not claim_key:
        raise AnonymisationRefused("claim_key is required")
    if not isinstance(secret, bytes) or not secret:
        raise AnonymisationRefused("a non-empty runtime secret is required")
    _reject_binary(bundle)
    _reject_ambiguous_free_text(bundle)
    output = deepcopy(bundle)
    fields = output.get("fields")
    if not isinstance(fields, list):
        raise AnonymisationRefused("bundle.fields must be a list")
    mapping: dict[tuple[str, str], str] = {}
    for field in fields:
        if not isinstance(field, dict):
            raise AnonymisationRefused("every field must be an object")
        value_type = field.get("value_type")
        value = field.get("value")
        kind = field.get("pii_kind")
        pii_class = field.get("pii_class")
        path = field.get("path")
        if value_type not in SCALAR_VALUE_TYPES:
            raise AnonymisationRefused("non-scalar or unknown field type is not exportable")
        if isinstance(value, (dict, list)):
            raise AnonymisationRefused("non-scalar field value is not exportable")
        if value_type == "money":
            if not isinstance(value, int) or isinstance(value, bool):
                raise AnonymisationRefused("money must be integer KES cents")
            if kind is not None:
                raise AnonymisationRefused("money cannot be classified as PII")
            continue
        if kind is not None:
            if kind not in PII_KINDS or not isinstance(value, str) or not value:
                raise AnonymisationRefused("unsupported or invalid PII classification")
            normalised = _normalise(value)
            cache_key = (kind, normalised)
            if cache_key not in mapping:
                digest = hmac.new(
                    secret,
                    f"{claim_key}\0{kind}\0{normalised}".encode(),
                    hashlib.sha256,
                ).digest()
                mapping[cache_key] = _pseudonym(kind, value, digest)
            field["value"] = mapping[cache_key]
            continue
        if pii_class not in {None, "none"}:
            raise AnonymisationRefused("PII field lacks a supported pii_kind")
        if isinstance(path, str) and PII_PATH.search(path):
            raise AnonymisationRefused("PII path lacks a supported pii_kind")
        if value_type == "string" and isinstance(path, str) and FREE_TEXT_PATH.search(path):
            raise AnonymisationRefused("ambiguous free text is not exportable")
    return output


def main(argv: list[str] | None = None) -> int:
    """Read structured JSON and atomically create a new anonymised JSON path."""

    parser = argparse.ArgumentParser(prog="python -m eval_harness.anonymise")
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--claim-key", required=True)
    args = parser.parse_args(argv)
    secret_text = os.environ.get("PACHA_ANONYMISATION_SECRET")
    if not secret_text:
        raise AnonymisationRefused("PACHA_ANONYMISATION_SECRET is required")
    if args.output.exists() or args.output.is_symlink():
        raise AnonymisationRefused("output already exists")
    try:
        source = json.loads(args.input.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AnonymisationRefused("input must be UTF-8 structured JSON") from error
    anonymised = anonymise_bundle(
        source,
        claim_key=args.claim_key,
        secret=secret_text.encode("utf-8"),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=args.output.parent,
            prefix=f".{args.output.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(anonymised, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, args.output)
        except FileExistsError as error:
            raise AnonymisationRefused("output appeared during anonymisation") from error
        temporary.unlink()
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a process contract
    raise SystemExit(main())


__all__ = ["AnonymisationRefused", "anonymise_bundle", "main"]
