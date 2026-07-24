"""Workflow-history privacy checks (master plan sections 10 and 22.5).

The control-only contract stops PII entering a payload; this module is how we
prove it afterwards. A test seeds sentinel values — a name, a policy number, a
registration plate, bank data, a money figure, document text, a credential, a
narrative — through the code path under test and then asserts that none of them
occurs anywhere in the history Temporal actually recorded.

Two scans are worth running and they prove different things:

* against a **codec-enabled** client the history comes back decoded, so a clean
  scan proves the plaintext minimisation rule itself — the sentinel was never
  in the payload, encrypted or not;
* against a **plain** client the history comes back as ciphertext, so a clean
  scan proves the Codec was actually applied end to end.

Scanning is done over the serialized protobuf rather than a rendered string, so
event attributes, failure messages, heartbeat details and headers are all
covered by one pass.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from google.protobuf.message import Message
from temporalio.api.common.v1 import Payload
from temporalio.api.history.v1 import History
from temporalio.converter import PayloadCodec

from orchestration.codec import CODEC_ENCODING
from orchestration.errors import HistoryPrivacyError

__all__ = [
    "assert_no_sentinels",
    "assert_workflow_history_private",
    "decoded_history_blob",
    "find_sentinels",
    "history_blob",
]


def _collect_payloads(message: Message, found: list[Payload]) -> None:
    """Depth-first walk collecting every `Payload` anywhere in a proto message.

    Payloads hide in singular fields, repeated fields and map values —
    Activity input, Workflow result, Signal arguments, heartbeat details,
    failure details and headers all differ — so the walk is generic rather than
    a hand-written list of the fields we happen to expect today.
    """

    if isinstance(message, Payload):
        found.append(message)
        return
    for _field, value in message.ListFields():
        if isinstance(value, Message):
            _collect_payloads(value, found)
        elif isinstance(value, (str, bytes)):
            continue
        elif hasattr(value, "values"):  # a protobuf map field
            for item in value.values():
                if isinstance(item, Message):
                    _collect_payloads(item, found)
        elif isinstance(value, Iterable):  # a repeated field
            for item in value:
                if isinstance(item, Message):
                    _collect_payloads(item, found)


async def decoded_history_blob(history: Any, codec: PayloadCodec) -> bytes:
    """Serialize a fetched history with every Pacha payload decrypted first.

    `WorkflowHandle.fetch_history` returns the history exactly as stored, which
    is ciphertext. Scanning that only proves the Codec ran. Decoding first is
    the check that matters: a clean scan then proves the sentinel was never in
    the payload to begin with, which is the rule AR-1 actually states.

    Payloads not written by this Codec are left untouched rather than refused,
    so an SDK-internal plaintext payload cannot mask the scan.
    """

    proto = History(events=list(getattr(history, "events", [])))
    payloads: list[Payload] = []
    _collect_payloads(proto, payloads)
    ours = [payload for payload in payloads if payload.metadata.get("encoding") == CODEC_ENCODING]
    if ours:
        for target, decoded in zip(ours, await codec.decode(ours), strict=True):
            target.CopyFrom(decoded)
    return proto.SerializeToString()


def history_blob(history: Any) -> bytes:
    """Serialize a fetched history to one scannable byte string.

    Accepts either the SDK's `WorkflowHistory` or anything else exposing an
    `events` sequence of protobuf events.
    """

    events: Sequence[Any] | None = getattr(history, "events", None)
    if events is None:
        raise HistoryPrivacyError("object has no history events to scan")
    return b"".join(event.SerializeToString() for event in events)


def find_sentinels(blob: bytes | str, sentinels: Iterable[str]) -> list[str]:
    """Return every sentinel present in `blob`, matched case-insensitively."""

    raw = blob.encode("utf-8") if isinstance(blob, str) else bytes(blob)
    lowered = raw.lower()
    found: list[str] = []
    for sentinel in sentinels:
        if not sentinel:
            continue
        needle = sentinel.encode("utf-8")
        if needle in raw or needle.lower() in lowered:
            found.append(sentinel)
    return found


def assert_no_sentinels(blob: bytes | str, sentinels: Iterable[str], *, source: str) -> None:
    """Raise if any sentinel appears in `blob`.

    The exception names the count and the source, never the value: a privacy
    failure must not be reported by repeating the leaked datum into a log.
    """

    found = find_sentinels(blob, sentinels)
    if found:
        raise HistoryPrivacyError(f"{len(found)} forbidden sentinel(s) found in {source}")


async def assert_workflow_history_private(
    handle: Any,
    sentinels: Iterable[str],
    *,
    codec: PayloadCodec | None = None,
) -> None:
    """Fetch a Workflow's history and assert it carries no seeded sentinel.

    Pass the client's Codec to scan the decoded history, which is the stronger
    assertion; without one the scan covers the stored ciphertext only.
    """

    history = await handle.fetch_history()
    if codec is None:
        blob = history_blob(history)
        source = "stored workflow history"
    else:
        blob = await decoded_history_blob(history, codec)
        source = "decoded workflow history"
    assert_no_sentinels(blob, sentinels, source=source)
