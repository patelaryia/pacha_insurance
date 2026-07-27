"""The control-only payload contract for everything crossing the Temporal edge.

Master plan section 10 (implementing Section 0.5 AR-1's hard workflow-history
rule) closes the payload surface completely: twenty allowlisted field names,
every one of them a string or integer, every one of them structurally closed to
a ULID, a UUID, a hexadecimal digest, a registry token, a Workflow identifier or
a non-negative integer.

The closure is the defence. A field that only accepts a 26-character Crockford
ULID cannot carry a name, a registration plate, a bank account, a money amount
or a sentence, so no heuristic redaction pass is needed to keep those out. The
`FORBIDDEN_CATEGORIES` scan below is a second, independent barrier applied to
the handful of sub-values whose charset is permissive enough to hold a word: it
exists so that a future widening of a registry cannot silently open a channel.

Nothing here reads or writes claim data. Contracts are frozen dataclasses that
validate on construction, so an invalid control payload cannot be built at all,
let alone sent.

**Where enforcement actually happens.** `ControlPayloadConverter` is the
completeness guarantee, not the client interceptor. An interceptor sees only the
outbound calls it names, so it can never cover a Workflow result, a Query
result, an Activity result, heartbeat details, headers or failure details — a
Workflow could return an arbitrary dictionary and the Codec would faithfully
encrypt it. The converter sits in the `DataConverter` ahead of serialization and
is therefore on every payload path in both directions. The interceptor remains,
narrower and sharper: it refuses SDK surfaces Pacha has not approved and pins
the duplicate-start policy.

The 8 KiB limit binds the **complete unencoded argument or result collection**,
not each argument separately, and the recursion stops the moment the running
total exceeds it, so an oversized payload costs bounded work to reject.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import yaml
from temporalio.api.common.v1 import Payload
from temporalio.client import (
    Interceptor,
    OutboundInterceptor,
    QueryWorkflowInput,
    SignalWorkflowInput,
    StartNexusOperationInput,
    StartWorkflowInput,
    StartWorkflowUpdateInput,
    StartWorkflowUpdateWithStartInput,
)
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.converter import DefaultPayloadConverter

from orchestration.errors import ControlContractError

__all__ = [
    "CONTROL_FIELDS",
    "CONTROL_STATUSES",
    "FORBIDDEN_CATEGORIES",
    "MAX_CONTROL_NESTING_DEPTH",
    "MAX_CONTROL_PAYLOAD_BYTES",
    "MAX_CONTROL_STRING_BYTES",
    "REQUIRED_ID_CONFLICT_POLICY",
    "REQUIRED_ID_REUSE_POLICY",
    "WORKFLOW_ID_PATTERN",
    "ControlCommand",
    "ControlHeartbeat",
    "ControlPayloadConverter",
    "ControlPayloadInterceptor",
    "ControlRegistries",
    "ControlResult",
    "ControlSignal",
    "control_payload_size",
    "load_control_registries",
    "scan_forbidden_categories",
    "validate_control_collection",
    "validate_control_field",
    "validate_control_payload",
]

# Section 10 — the complete allowlist, in the order the master plan states it.
CONTROL_FIELDS: tuple[str, ...] = (
    "run_ref",
    "claim_ref",
    "workflow_ref",
    "workflow_run_ref",
    "trigger_event_ref",
    "event_ref",
    "event_seq",
    "review_event_ref",
    "document_ref",
    "checklist_ref",
    "projection_ref",
    "schedule_ref",
    "pack_version",
    "payload_hash",
    "write_id",
    "step_id",
    "status",
    "wake_at_epoch_ms",
    "timer_seconds",
    "attempt_no",
)

MAX_CONTROL_STRING_BYTES = 160
MAX_CONTROL_PAYLOAD_BYTES = 8 * 1024

#: A control payload is a contract, a mapping of control fields, or a list of
#: those. Anything deeper is a structure, and section 10 permits no structures.
MAX_CONTROL_NESTING_DEPTH = 4

#: Section 9 start policy. Every start must pin both, so a retried start
#: attaches to the execution the committed Pacha ULID already identifies.
REQUIRED_ID_REUSE_POLICY = WorkflowIDReusePolicy.REJECT_DUPLICATE
REQUIRED_ID_CONFLICT_POLICY = WorkflowIDConflictPolicy.USE_EXISTING

_ULID_FIELDS: frozenset[str] = frozenset(
    {
        "run_ref",
        "claim_ref",
        "trigger_event_ref",
        "event_ref",
        "review_event_ref",
        "document_ref",
        "checklist_ref",
        "projection_ref",
    }
)

_INTEGER_FIELDS: frozenset[str] = frozenset(
    {"event_seq", "wake_at_epoch_ms", "timer_seconds", "attempt_no"}
)

# Crockford base32, 26 characters, first character 0-7 (48-bit timestamp bound).
_ULID_PATTERN = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
_ULID_LOWER_PATTERN = re.compile(r"^[0-7][0-9a-hjkmnp-tv-z]{25}$")
_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_PAYLOAD_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")

# Section 9 — the exact Workflow-ID forms. `parse_workflow_ref` in `ids` is the
# only supported way to take one apart; this pattern is the only way to admit
# one as a payload value.
WORKFLOW_ID_KINDS: tuple[str, ...] = (
    "agent",
    "chase",
    "docintel",
    "intake",
    "assessment",
    "approval-pack",
    "projection",
)
WORKFLOW_ID_PATTERN = re.compile(
    r"^pacha\.(" + "|".join(re.escape(kind) for kind in WORKFLOW_ID_KINDS) + r")\."
    r"[0-7][0-9A-HJKMNP-TV-Z]{25}$"
)

# Section 16 — stable Schedule IDs are `pacha-{env}-{job}-v1`.
_SCHEDULE_REF_PATTERN = re.compile(r"^pacha-(?:dev|test|staging|prod)-[a-z0-9]+(?:-[a-z0-9]+)*-v1$")

# Section 10 — the spec's literal charset for a write ID.
_WRITE_ID_CHARSET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9:._-]{0,159}$")
# ... and its stated construction: a fixed operation name plus opaque ULIDs and
# integers. Enforcing the construction as well as the charset is what stops a
# caller assembling a write ID out of anything else the charset happens to fit.
_WRITE_ID_OPERATION_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)*$")

# Section 0.5 AR-1 / master plan section 13 — the closed `agent_runs` status set,
# which is DDL-level constitution rather than pack data.
CONTROL_STATUSES: frozenset[str] = frozenset(
    {
        "pending",
        "running",
        "awaiting_review",
        "blocked",
        "completed",
        "failed",
        "cancelled",
    }
)

# The second barrier. Each pattern is chosen so that no structurally valid
# control token can match it: they run only against registry tokens and the
# operation-name segment of a write ID, all of which are `[a-z0-9._-]` words.
FORBIDDEN_CATEGORIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("party_details", re.compile(r"@|\+\d|\b(?:mr|mrs|ms|dr|insured|claimant)\b")),
    ("postal_address", re.compile(r"\b(?:po_?box|street|road|avenue|estate|apartment)\b")),
    ("policy_or_registration", re.compile(r"\bk[a-z]{2}\d{3}[a-z]\b|\bpolicy\b|\bregistration\b")),
    ("identity_or_bank", re.compile(r"\b(?:kra|pin|passport|iban|swift|account|nationalid)\b")),
    ("document_or_extracted_fact", re.compile(r"\b(?:anchor|citation|extracted|ocr_text)\b")),
    ("money_or_settlement", re.compile(r"\b(?:kes|ksh|amount|reserve|payable|settlementvalue)\b")),
    ("narrative_or_prose", re.compile(r"\s")),
    ("recipient_list", re.compile(r"\b(?:recipients?|to_party|cc|bcc|mailto)\b")),
    ("model_payload", re.compile(r"\b(?:prompt|completion|llm_input|llm_output)\b")),
    ("credential", re.compile(r"\b(?:secret|token|password|apikey|bearer)\b")),
    ("raw_error", re.compile(r"\b(?:traceback|exception|stacktrace)\b")),
)


@dataclass(frozen=True, slots=True)
class ControlRegistries:
    """The closed registries backing `step_id`, `status` and `pack_version`.

    Statuses are constitution and live in code. Step IDs and pack versions are
    pack data (guide §4, config over code) and are read from the pack.
    """

    step_ids: frozenset[str]
    pack_versions: frozenset[str]
    statuses: frozenset[str] = CONTROL_STATUSES


def _pack_root() -> Path:
    return Path(__file__).resolve().parents[2] / "packs" / "motor"


@functools.lru_cache(maxsize=8)
def load_control_registries(pack_root: Path | None = None) -> ControlRegistries:
    """Read the closed `step_id` and `pack_version` registries from the pack."""

    root = pack_root or _pack_root()
    try:
        steps_document = yaml.safe_load((root / "cop_steps.yaml").read_text(encoding="utf-8"))
        pack_document = yaml.safe_load((root / "pack.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:  # pragma: no cover - unreadable pack
        raise ControlContractError("registries", f"pack registries unreadable: {error}") from error

    step_ids: set[str] = set()
    for definition in (steps_document or {}).get("step_definitions", []):
        for step in definition.get("steps", []):
            step_id = step.get("id")
            if isinstance(step_id, str):
                step_ids.add(step_id)

    version = (pack_document or {}).get("version")
    pack_versions = {version} if isinstance(version, str) else set()

    return ControlRegistries(
        step_ids=frozenset(step_ids),
        pack_versions=frozenset(pack_versions),
    )


def scan_forbidden_categories(text: str) -> str | None:
    """Return the first forbidden-data category `text` matches, if any.

    Applied only to permissive sub-values (registry tokens and the operation
    name of a write ID). Structurally closed fields — ULIDs, UUIDs, digests,
    Workflow IDs — are not scanned: they cannot express any of these categories,
    and a digit run inside a valid ULID must never be mistaken for one.
    """

    lowered = text.lower()
    for category, pattern in FORBIDDEN_CATEGORIES:
        if pattern.search(lowered):
            return category
    return None


def _reject_forbidden(field: str, text: str) -> None:
    category = scan_forbidden_categories(text)
    if category is not None:
        raise ControlContractError(field, f"forbidden data category {category!r}")


def _validate_write_id(field: str, value: str) -> None:
    if not _WRITE_ID_CHARSET_PATTERN.fullmatch(value):
        raise ControlContractError(field, "not a permitted write-ID charset")
    operation, _, remainder = value.partition(":")
    if not remainder:
        raise ControlContractError(field, "write ID needs an opaque ULID or integer segment")
    if not _WRITE_ID_OPERATION_PATTERN.fullmatch(operation):
        raise ControlContractError(field, "write ID operation name is not a fixed identifier")
    _reject_forbidden(field, operation)
    for segment in remainder.split(":"):
        if _ULID_LOWER_PATTERN.fullmatch(segment):
            continue
        if segment == "0" or (segment.isdigit() and not segment.startswith("0")):
            continue
        raise ControlContractError(field, "write ID segment is not an opaque ULID or integer")


def validate_control_field(field: str, value: Any) -> None:
    """Validate one allowlisted control field, or refuse the field outright.

    Raises:
        ControlContractError: unknown field name, wrong type, oversized value or
            a value that does not match the closed format for that field.
    """

    if field not in CONTROL_FIELDS:
        raise ControlContractError(field, "not an allowlisted control field")

    if field in _INTEGER_FIELDS:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ControlContractError(field, "must be an integer")
        if value < 0:
            raise ControlContractError(field, "must be non-negative")
        return

    if not isinstance(value, str):
        raise ControlContractError(field, "must be a string")
    if len(value.encode("utf-8")) > MAX_CONTROL_STRING_BYTES:
        raise ControlContractError(
            field, f"longer than {MAX_CONTROL_STRING_BYTES} UTF-8 bytes"
        )

    if field in _ULID_FIELDS:
        if not _ULID_PATTERN.fullmatch(value):
            raise ControlContractError(field, "not an uppercase 26-character ULID")
        return
    if field == "workflow_ref":
        if not WORKFLOW_ID_PATTERN.fullmatch(value):
            raise ControlContractError(field, "not a declared Workflow-ID form")
        return
    if field == "workflow_run_ref":
        if not _UUID_PATTERN.fullmatch(value):
            raise ControlContractError(field, "not a canonical UUID")
        return
    if field == "payload_hash":
        if not _PAYLOAD_HASH_PATTERN.fullmatch(value):
            raise ControlContractError(field, "not 64 lowercase hexadecimal characters")
        return
    if field == "schedule_ref":
        if not _SCHEDULE_REF_PATTERN.fullmatch(value):
            raise ControlContractError(field, "not a declared Schedule-ID form")
        return
    if field == "write_id":
        _validate_write_id(field, value)
        return

    registries = load_control_registries()
    if field == "status":
        _reject_forbidden(field, value)
        if value not in registries.statuses:
            raise ControlContractError(field, "not a registered run status")
        return
    if field == "step_id":
        _reject_forbidden(field, value)
        if value not in registries.step_ids:
            raise ControlContractError(field, "not a registered COP step")
        return
    if field == "pack_version":
        _reject_forbidden(field, value)
        if value not in registries.pack_versions:
            raise ControlContractError(field, "not a registered pack version")
        return

    raise ControlContractError(field, "no validator registered")  # pragma: no cover


class _Budget:
    """A running byte total for one argument or result collection.

    Charging exactly what the canonical JSON encoding costs means the running
    total *is* the unencoded size, so the limit can be enforced during the walk
    rather than after building a structure that may already be enormous.
    """

    __slots__ = ("limit", "used")

    def __init__(self, limit: int = MAX_CONTROL_PAYLOAD_BYTES) -> None:
        self.limit = limit
        self.used = 0

    def spend(self, field: str, cost: int) -> None:
        self.used += cost
        if self.used > self.limit:
            raise ControlContractError(
                field,
                f"the unencoded payload collection exceeds {self.limit} bytes",
            )


def _json_len(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":")).encode("utf-8"))


def _measure(value: Any, field: str, budget: _Budget, depth: int) -> None:
    """Validate one value and charge its exact serialized cost to `budget`."""

    if depth > MAX_CONTROL_NESTING_DEPTH:
        raise ControlContractError(field, "nested deeper than a control payload may be")

    if value is None or isinstance(value, (bool, int)):
        raise ControlContractError(
            field,
            "unnamed scalars may not cross the Temporal boundary; "
            "use a declared control field",
        )
    if isinstance(value, _ControlContract):
        _measure_mapping(value.as_control_mapping(), field, budget, depth)
        return
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        raise ControlContractError(field, "not a declared control contract type")
    if isinstance(value, Mapping):
        _measure_mapping(value, field, budget, depth)
        return
    if isinstance(value, (str, bytes, bytearray)):
        raise ControlContractError(field, "bare strings may not cross the Temporal boundary")
    if isinstance(value, Sequence):
        budget.spend(field, 2 + max(len(value) - 1, 0))  # "[" "]" and commas
        for index, item in enumerate(value):
            _measure(item, f"{field}[{index}]", budget, depth + 1)
        return
    raise ControlContractError(field, "not a control contract, mapping or sequence")


def _measure_mapping(mapping: Mapping[Any, Any], field: str, budget: _Budget, depth: int) -> None:
    budget.spend(field, 2 + max(len(mapping) - 1, 0))  # "{" "}" and commas
    for key, item in mapping.items():
        if not isinstance(key, str):
            raise ControlContractError(field, "mapping keys must be control field names")
        validate_control_field(key, item)
        budget.spend(field, _json_len(key) + 1 + _json_len(item))  # key, ":", value


def control_payload_size(values: Sequence[Any]) -> int:
    """The exact unencoded size of a validated argument/result collection."""

    budget = _Budget(limit=MAX_CONTROL_PAYLOAD_BYTES)
    budget.spend("payload", 2 + max(len(values) - 1, 0))
    for index, value in enumerate(values):
        _measure(value, f"payload[{index}]", budget, 0)
    return budget.used


class _ControlContract:
    """Shared validation for every frozen control dataclass."""

    def _validate(self) -> None:
        budget = _Budget()
        for field in fields(self):  # type: ignore[arg-type]
            value = getattr(self, field.name)
            if value is None:
                continue
            validate_control_field(field.name, value)
        _measure_mapping(self.as_control_mapping(), type(self).__name__, budget, 0)

    def as_control_mapping(self) -> dict[str, Any]:
        """The populated allowlisted fields, for logging and assertions."""

        return {
            field.name: getattr(self, field.name)
            for field in fields(self)  # type: ignore[arg-type]
            if getattr(self, field.name) is not None
        }


@dataclass(frozen=True, slots=True)
class ControlCommand(_ControlContract):
    """Opaque control input to a Workflow or Activity.

    Every field is an allowlisted section 10 reference. An Activity receiving
    one loads the authoritative record from PostgreSQL; the command itself
    carries no claim fact.
    """

    run_ref: str
    claim_ref: str | None = None
    workflow_ref: str | None = None
    workflow_run_ref: str | None = None
    trigger_event_ref: str | None = None
    event_ref: str | None = None
    event_seq: int | None = None
    review_event_ref: str | None = None
    document_ref: str | None = None
    checklist_ref: str | None = None
    projection_ref: str | None = None
    schedule_ref: str | None = None
    pack_version: str | None = None
    payload_hash: str | None = None
    write_id: str | None = None
    step_id: str | None = None
    attempt_no: int | None = None

    def __post_init__(self) -> None:
        self._validate()


@dataclass(frozen=True, slots=True)
class ControlResult(_ControlContract):
    """Opaque control disposition returned by a Workflow or Activity."""

    status: str
    run_ref: str | None = None
    claim_ref: str | None = None
    event_ref: str | None = None
    event_seq: int | None = None
    review_event_ref: str | None = None
    projection_ref: str | None = None
    payload_hash: str | None = None
    write_id: str | None = None
    step_id: str | None = None
    wake_at_epoch_ms: int | None = None
    timer_seconds: int | None = None
    attempt_no: int | None = None

    def __post_init__(self) -> None:
        self._validate()


@dataclass(frozen=True, slots=True)
class ControlSignal(_ControlContract):
    """A Signal body: exactly one opaque event reference (section 15)."""

    event_ref: str

    def __post_init__(self) -> None:
        self._validate()


@dataclass(frozen=True, slots=True)
class ControlHeartbeat(_ControlContract):
    """Activity heartbeat detail: stage and attempt integers only."""

    step_id: str
    attempt_no: int
    event_seq: int | None = None

    def __post_init__(self) -> None:
        self._validate()


CONTROL_CONTRACT_TYPES: tuple[type, ...] = (
    ControlCommand,
    ControlResult,
    ControlSignal,
    ControlHeartbeat,
)


def validate_control_collection(values: Sequence[Any], *, field: str = "payload") -> None:
    """Validate a complete argument or result collection against section 10.

    The whole collection shares one 8 KiB budget, because that is the unit the
    limit is stated in and because validating arguments independently lets any
    number of individually legal arguments add up to an illegal payload.
    """

    budget = _Budget()
    budget.spend(field, 2 + max(len(values) - 1, 0))
    for index, value in enumerate(values):
        _measure(value, f"{field}[{index}]", budget, 0)


def validate_control_payload(value: Any, *, field: str = "payload") -> None:
    """Recursively refuse anything that is not a closed control value.

    Accepts a control contract instance, an allowlisted scalar mapping or a
    sequence of those. Bare scalars are rejected even when they are integers:
    without an allowlisted field name, the boundary cannot distinguish an event
    sequence from a forbidden money amount. Everything else — an arbitrary
    dictionary, a free-form string, a float, a nested object or a byte string —
    is rejected because section 10 permits no contract that carries one.
    """

    _measure(value, field, _Budget(), 0)


class ControlPayloadConverter(DefaultPayloadConverter):
    """The mandatory validating converter, ahead of every serialization.

    This is where the control-only rule is actually complete. It runs for
    Workflow input and result, Signal and Query arguments, Query results,
    Activity input and result, heartbeat details, headers and failure details —
    every path a payload can take — because the SDK routes all of them through
    the `DataConverter`'s payload converter before the Codec ever sees bytes.

    `with_context` on the base class clones via a nullary constructor, so a
    context-scoped clone is still a `ControlPayloadConverter` and validation
    cannot be shed part-way through a call.
    """

    def to_payloads(self, values: Sequence[Any]) -> list[Payload]:
        validate_control_collection(values)
        return super().to_payloads(values)


def _validate_args(args: Iterable[Any], *, field: str) -> None:
    validate_control_collection(list(args), field=field)


def _refuse_metadata(input: Any) -> None:
    """Refuse the SDK's free-text and indexing surfaces (section 10)."""

    for attribute, message in (
        ("memo", "memo is not permitted in v1"),
        ("search_attributes", "custom search attributes are not permitted in v1"),
        ("static_summary", "static summary is not permitted in v1"),
        ("static_details", "static details are not permitted in v1"),
        ("headers", "custom headers are not permitted in v1"),
    ):
        if getattr(input, attribute, None):
            raise ControlContractError(attribute, message)


class _ControlOutbound(OutboundInterceptor):
    """Refuse any outbound SDK call carrying a non-control payload.

    Narrower than the converter and doing a different job: it polices the SDK
    surfaces Pacha has approved, and it pins the duplicate-start policy that
    makes a retried start attach instead of duplicating domain work.
    """

    async def start_workflow(self, input: StartWorkflowInput):  # type: ignore[override]
        validate_control_field("workflow_ref", input.id)
        _refuse_metadata(input)
        if input.id_reuse_policy is not REQUIRED_ID_REUSE_POLICY:
            raise ControlContractError(
                "id_reuse_policy", "every start must use WorkflowIDReusePolicy.REJECT_DUPLICATE"
            )
        if input.id_conflict_policy is not REQUIRED_ID_CONFLICT_POLICY:
            raise ControlContractError(
                "id_conflict_policy",
                "every start must use WorkflowIDConflictPolicy.USE_EXISTING",
            )
        if input.cron_schedule:
            raise ControlContractError(
                "cron_schedule", "cron starts are not permitted; recurring work uses Schedules"
            )
        _validate_args(input.args, field="workflow_args")
        _validate_args(input.start_signal_args, field="start_signal_args")
        return await super().start_workflow(input)

    async def signal_workflow(self, input: SignalWorkflowInput) -> None:  # type: ignore[override]
        validate_control_field("workflow_ref", input.id)
        _refuse_metadata(input)
        _validate_args(input.args, field="signal_args")
        await super().signal_workflow(input)

    async def query_workflow(self, input: QueryWorkflowInput) -> Any:  # type: ignore[override]
        validate_control_field("workflow_ref", input.id)
        _refuse_metadata(input)
        _validate_args(input.args, field="query_args")
        return await super().query_workflow(input)

    async def start_workflow_update(self, input: StartWorkflowUpdateInput) -> Any:
        raise ControlContractError("workflow_update", "Workflow Updates are not approved in v1")

    async def start_update_with_start_workflow(
        self, input: StartWorkflowUpdateWithStartInput
    ) -> Any:
        raise ControlContractError("workflow_update", "Workflow Updates are not approved in v1")

    async def start_nexus_operation(self, input: StartNexusOperationInput) -> Any:
        raise ControlContractError("nexus_operation", "Nexus operations are not approved in v1")


class ControlPayloadInterceptor(Interceptor):
    """Client interceptor enforcing section 10 before any SDK call leaves Pacha.

    Validation failure raises in the caller's transaction, not in Temporal
    history, which is the whole point: a rejected payload never existed.
    """

    def intercept_client(self, next: OutboundInterceptor) -> OutboundInterceptor:
        return _ControlOutbound(next)
