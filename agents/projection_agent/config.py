"""Fail-closed PRD-09 operation catalogue and click-path loader (registers #263/#265).

The catalogue is pack data. Nothing here executes a browser action: PACKET-20
parses the human-facing subset of the same versioned click-path definition that
PACKET-21 will later execute, so there is no second field-order registry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from claim_core import field_dictionary

#: PRD-09 §9.2 registers exactly these fifteen v1 operations.
OPERATION_IDS: tuple[str, ...] = (
    "icon.policy_read",
    "icon.claim_register",
    "icon.reserve_create",
    "icon.reserve_breakdown",
    "icon.reserve_adjust",
    "icon.assessor_payment_request",
    "icon.note_entry",
    "icon.claim_details_report",
    "icon.salvage_register",
    "icon.payment_voucher",
    "edms.general_payments",
    "edms.claims_workflow",
    "edms.attach_and_tag",
    "edms.claim_payment",
    "edms.payment_workflow",
)

SYSTEMS = frozenset({"icon", "edms"})
MODES = frozenset({"paste_assist", "rpa", "api"})
AVAILABILITY = frozenset({"live", "pending_capture", "blocked_on_inputs"})
OWNER_PRDS = frozenset({"PRD-09", "PRD-11", "PRD-12"})
OPERATION_KEYS = frozenset(
    {
        "id",
        "version",
        "system",
        "mode",
        "status",
        "blocked_on",
        "click_path_ref",
        "owner_prd",
    }
)
ROOT_KEYS = frozenset({"version", "paste_readback_sampling", "operations"})
SAMPLING_KEYS = frozenset({"rate_percent", "schedule"})
SCHEDULE_KEYS = frozenset({"day_of_week", "hour", "minute", "timezone"})
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

CLICK_PATH_KEYS = frozenset(
    {
        "operation",
        "version",
        "status",
        "preconditions",
        "screens",
        "steps",
        "readback",
        "validators",
        "failure_policy",
    }
)
SCREEN_KEYS = frozenset({"id", "label", "order"})
STEP_KEYS = frozenset(
    {
        "id",
        "screen",
        "action",
        "selector",
        "value",
        "value_kind",
        "rule",
        "rule_values",
        "external_encoding",
        "paste_assist",
    }
)
PASTE_KEYS = frozenset({"label", "copy"})
READBACK_KEYS = frozenset({"capture", "label", "into", "assert_format", "required"})
VALIDATOR_KEYS = frozenset({"status", "pattern", "blocked_on"})
#: PRD-09 §9.4 uses exactly these three step actions. An unknown action is a
#: capture defect, never something to interpret.
ACTIONS = frozenset({"click", "fill", "select"})
COPY_ACTIONS = frozenset({"fill", "select"})
#: Every binding kind a *live* production path may declare. `generated` and
#: `value_map` have no captured declaration format, so a live path using one is
#: rejected rather than guessed (guide §3.5).
VALUE_KINDS = frozenset({"field", "rule", "literal"})
ENCODINGS_BY_TYPE: dict[str, frozenset[str]] = {
    "string": frozenset({"raw"}),
    "enum": frozenset({"raw"}),
    "money": frozenset({"cents", "shillings"}),
    "date": frozenset({"iso"}),
    "datetime": frozenset({"iso"}),
}
SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
FIELD_PLACEHOLDER = re.compile(r"^\{([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)\}$")
IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")


class OperationConfigError(ValueError):
    """The catalogue or a click path is unusable; startup must fail closed."""


@dataclass(frozen=True)
class Screen:
    """One target-form screen used to group the paste strip."""

    id: str
    label: str
    order: int


@dataclass(frozen=True)
class Step:
    """One parsed click-path step. Selectors are validated, never executed."""

    id: str
    screen: str
    action: str
    selector: str
    value_kind: str | None
    field_path: str | None
    rule_id: str | None
    rule_values: dict[str, str] | None
    literal: str | None
    external_encoding: str | None
    label: str | None
    copy: bool

    @property
    def is_copy_row(self) -> bool:
        return self.action in COPY_ACTIONS and self.value_kind is not None and self.copy


@dataclass(frozen=True)
class Readback:
    """One declared inline readback capture and its captured format validator."""

    capture: str
    label: str
    into: str
    assert_format: str
    required: bool


@dataclass(frozen=True)
class Validator:
    """A pack-declared readback format. `pending_capture` refuses every value."""

    name: str
    status: str
    pattern: re.Pattern[str] | None
    blocked_on: str | None


@dataclass(frozen=True)
class ClickPath:
    """The human-facing subset of one versioned operation definition."""

    operation: str
    version: str
    status: str
    screens: tuple[Screen, ...]
    steps: tuple[Step, ...]
    readback: tuple[Readback, ...]
    validators: dict[str, Validator] = field(default_factory=dict)

    def steps_for(self, screen_id: str) -> tuple[Step, ...]:
        return tuple(step for step in self.steps if step.screen == screen_id)


@dataclass(frozen=True)
class Operation:
    """One catalogue row plus its resolved click path when the row is live."""

    id: str
    version: str
    system: str
    mode: str
    status: str
    blocked_on: str | None
    click_path_ref: str | None
    owner_prd: str
    click_path: ClickPath | None

    @property
    def capability_id(self) -> str:
        """Canonical PRD-09 §9.6 capability id. Never the bare operation id."""

        return f"project.{self.id}"

    def catalogue_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "capability_id": self.capability_id,
            "system": self.system,
            "mode": self.mode,
            "status": self.status,
            "blocked_on": self.blocked_on,
            "owner_prd": self.owner_prd,
            "version": self.version,
        }


@dataclass(frozen=True)
class SamplingConfig:
    """Pack-configured weekly paste readback sampling (register #271)."""

    rate_percent: int
    day_of_week: str
    hour: int
    minute: int
    timezone: str


def _mapping(value: Any, message: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OperationConfigError(message)
    return value


def _text(value: Any, message: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OperationConfigError(message)
    return value


def _int(value: Any, message: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise OperationConfigError(message)
    return value


def _load_yaml(path: Path, label: str) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise OperationConfigError(f"invalid {label}: {error}") from error


def _resolve_ref(root: Path, ref: str) -> Path:
    """Resolve a repository-relative click-path ref inside the operation root."""

    if ref.startswith("/") or "\\" in ref:
        raise OperationConfigError(f"click_path_ref {ref!r} must be a relative POSIX path")
    candidate = (root / ref).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise OperationConfigError(f"click_path_ref {ref!r} escapes the operation root")
    if not candidate.is_file():
        raise OperationConfigError(f"click_path_ref {ref!r} does not exist")
    return candidate


def _parse_sampling(raw: Any) -> SamplingConfig:
    values = _mapping(raw, "paste_readback_sampling must be a mapping")
    unknown = set(values) - SAMPLING_KEYS
    if unknown:
        raise OperationConfigError(f"unknown sampling keys {sorted(unknown)}")
    rate = _int(values.get("rate_percent"), "sampling rate_percent must be an integer")
    if not 0 <= rate <= 100:
        raise OperationConfigError("sampling rate_percent must be between 0 and 100")
    schedule = _mapping(values.get("schedule"), "sampling schedule must be a mapping")
    if set(schedule) != SCHEDULE_KEYS:
        raise OperationConfigError("sampling schedule keys are invalid")
    day = _text(schedule.get("day_of_week"), "schedule day_of_week is required").lower()
    if day not in DAYS:
        raise OperationConfigError(f"unknown schedule day_of_week {day!r}")
    hour = _int(schedule.get("hour"), "schedule hour must be an integer")
    minute = _int(schedule.get("minute"), "schedule minute must be an integer")
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise OperationConfigError("schedule hour/minute are out of range")
    timezone = _text(schedule.get("timezone"), "schedule timezone is required")
    try:
        ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise OperationConfigError(
            f"schedule timezone {timezone!r} is not registered"
        ) from error
    return SamplingConfig(
        rate_percent=rate,
        day_of_week=day,
        hour=hour,
        minute=minute,
        timezone=timezone,
    )


def _parse_screens(raw: Any) -> tuple[Screen, ...]:
    if not isinstance(raw, list) or not raw:
        raise OperationConfigError("click path requires a non-empty screens list")
    screens: list[Screen] = []
    seen_ids: set[str] = set()
    seen_orders: set[int] = set()
    for entry in raw:
        values = _mapping(entry, "screen must be a mapping")
        if set(values) != SCREEN_KEYS:
            raise OperationConfigError(f"screen keys are invalid: {sorted(values)}")
        screen_id = _text(values["id"], "screen id is required")
        if IDENTIFIER.fullmatch(screen_id) is None:
            raise OperationConfigError(f"screen id {screen_id!r} is not a valid identifier")
        if screen_id in seen_ids:
            raise OperationConfigError(f"duplicate screen id {screen_id!r}")
        label = _text(values["label"], f"screen {screen_id!r} label is required")
        order = _int(values["order"], f"screen {screen_id!r} order must be an integer")
        if order in seen_orders:
            raise OperationConfigError(f"duplicate screen order {order}")
        seen_ids.add(screen_id)
        seen_orders.add(order)
        screens.append(Screen(id=screen_id, label=label, order=order))
    ordered = tuple(sorted(screens, key=lambda screen: screen.order))
    if [screen.order for screen in ordered] != list(range(1, len(ordered) + 1)):
        raise OperationConfigError("screen orders must be contiguous from 1")
    return ordered


def _parse_value_binding(step_id: str, values: dict[str, Any]) -> dict[str, Any]:
    """Resolve exactly one declared binding, or none, for one step."""

    raw_value = values.get("value")
    rule_id = values.get("rule")
    declared_kind = values.get("value_kind")
    if declared_kind is not None and declared_kind not in VALUE_KINDS:
        raise OperationConfigError(
            f"step {step_id!r} value_kind {declared_kind!r} is not a declared binding kind"
        )
    if rule_id is not None and raw_value is not None:
        raise OperationConfigError(f"step {step_id!r} declares two value bindings")
    if rule_id is not None:
        rule = _text(rule_id, f"step {step_id!r} rule must be a rule id")
        rule_values = _mapping(
            values.get("rule_values"),
            f"step {step_id!r} rule binding requires a declared rule_values map",
        )
        if set(rule_values) != {"true", "false"} or not all(
            isinstance(item, str) and item for item in rule_values.values()
        ):
            raise OperationConfigError(
                f"step {step_id!r} rule_values must declare exact true/false strings"
            )
        return {
            "value_kind": "rule",
            "field_path": None,
            "rule_id": rule,
            "rule_values": dict(rule_values),
            "literal": None,
        }
    if raw_value is None:
        if declared_kind is not None:
            raise OperationConfigError(f"step {step_id!r} declares a kind with no value")
        return {
            "value_kind": None,
            "field_path": None,
            "rule_id": None,
            "rule_values": None,
            "literal": None,
        }
    text_value = _text(raw_value, f"step {step_id!r} value must be a non-empty string")
    placeholder = FIELD_PLACEHOLDER.fullmatch(text_value)
    if placeholder is not None:
        path = placeholder.group(1)
        if declared_kind not in {None, "field"}:
            raise OperationConfigError(f"step {step_id!r} value_kind contradicts its value")
        if path.startswith("generated."):
            raise OperationConfigError(
                f"step {step_id!r} binds a generated value; generated values must be "
                "explicitly declared by captured configuration"
            )
        if path not in field_dictionary():
            raise OperationConfigError(
                f"step {step_id!r} binds unregistered field path {path!r}"
            )
        return {
            "value_kind": "field",
            "field_path": path,
            "rule_id": None,
            "rule_values": None,
            "literal": None,
        }
    if declared_kind != "literal":
        # `{generated.*}`, ternaries, and `value_map` refs have no captured
        # declaration format. A live path may not smuggle one in as a literal.
        raise OperationConfigError(
            f"step {step_id!r} value {text_value!r} is not a registered field binding; "
            "generated values, value maps, and literals must be explicitly declared"
        )
    if "{" in text_value or "}" in text_value:
        raise OperationConfigError(
            f"step {step_id!r} literal must not contain a placeholder expression"
        )
    return {
        "value_kind": "literal",
        "field_path": None,
        "rule_id": None,
        "rule_values": None,
        "literal": text_value,
    }


def _check_encoding(step_id: str, binding: dict[str, Any], encoding: Any) -> str | None:
    """Refuse a copy row whose target encoding was never captured (§4/#265)."""

    kind = binding["value_kind"]
    if kind is None:
        if encoding is not None:
            raise OperationConfigError(f"step {step_id!r} declares an encoding with no value")
        return None
    if kind == "field":
        definition = field_dictionary()[binding["field_path"]]
        allowed = ENCODINGS_BY_TYPE.get(definition.value_type)
        if allowed is None:
            raise OperationConfigError(
                f"step {step_id!r} binds value_type {definition.value_type!r}, which has "
                "no captured target encoding"
            )
        if encoding is None:
            raise OperationConfigError(
                f"step {step_id!r} must declare an external_encoding for "
                f"{binding['field_path']!r}"
            )
        if encoding not in allowed:
            raise OperationConfigError(
                f"step {step_id!r} external_encoding {encoding!r} is not declared for "
                f"value_type {definition.value_type!r}"
            )
        return str(encoding)
    if encoding not in {None, "raw"}:
        raise OperationConfigError(
            f"step {step_id!r} {kind} binding supports only the raw encoding"
        )
    return "raw"


def _parse_steps(raw: Any, screens: tuple[Screen, ...]) -> tuple[Step, ...]:
    if not isinstance(raw, list) or not raw:
        raise OperationConfigError("click path requires a non-empty steps list")
    known_screens = {screen.id for screen in screens}
    seen: set[str] = set()
    steps: list[Step] = []
    for entry in raw:
        values = _mapping(entry, "step must be a mapping")
        unknown = set(values) - STEP_KEYS
        if unknown:
            raise OperationConfigError(f"unknown step keys {sorted(unknown)}")
        step_id = _text(values.get("id"), "step id is required")
        if step_id in seen:
            raise OperationConfigError(f"duplicate step id {step_id!r}")
        seen.add(step_id)
        screen = _text(values.get("screen"), f"step {step_id!r} requires a screen")
        if screen not in known_screens:
            raise OperationConfigError(f"step {step_id!r} references unknown screen {screen!r}")
        action = _text(values.get("action"), f"step {step_id!r} requires an action")
        if action not in ACTIONS:
            raise OperationConfigError(f"step {step_id!r} has unknown action {action!r}")
        selector = _text(values.get("selector"), f"step {step_id!r} requires a selector")
        binding = _parse_value_binding(step_id, values)
        encoding = _check_encoding(step_id, binding, values.get("external_encoding"))
        paste = values.get("paste_assist")
        label: str | None = None
        copy = False
        if paste is not None:
            paste_values = _mapping(paste, f"step {step_id!r} paste_assist must be a mapping")
            unknown_paste = set(paste_values) - PASTE_KEYS
            if unknown_paste:
                raise OperationConfigError(f"unknown paste_assist keys {sorted(unknown_paste)}")
            label = _text(
                paste_values.get("label"), f"step {step_id!r} paste label is required"
            )
            copy_flag = paste_values.get("copy", False)
            if not isinstance(copy_flag, bool):
                raise OperationConfigError(f"step {step_id!r} paste copy must be boolean")
            copy = copy_flag
        if copy and binding["value_kind"] is None:
            raise OperationConfigError(f"step {step_id!r} is a copy row with no value binding")
        if copy and action not in COPY_ACTIONS:
            raise OperationConfigError(
                f"step {step_id!r} action {action!r} cannot be a copy row"
            )
        if binding["value_kind"] is not None and action in COPY_ACTIONS and label is None:
            raise OperationConfigError(f"step {step_id!r} value binding requires a paste label")
        steps.append(
            Step(
                id=step_id,
                screen=screen,
                action=action,
                selector=selector,
                value_kind=binding["value_kind"],
                field_path=binding["field_path"],
                rule_id=binding["rule_id"],
                rule_values=binding["rule_values"],
                literal=binding["literal"],
                external_encoding=encoding,
                label=label,
                copy=copy,
            )
        )
    return tuple(steps)


def _parse_validators(raw: Any) -> dict[str, Validator]:
    if raw is None:
        return {}
    values = _mapping(raw, "click path validators must be a mapping")
    parsed: dict[str, Validator] = {}
    for name, entry in values.items():
        if not isinstance(name, str) or not name:
            raise OperationConfigError("validator name must be a non-empty string")
        body = _mapping(entry, f"validator {name!r} must be a mapping")
        unknown = set(body) - VALIDATOR_KEYS
        if unknown:
            raise OperationConfigError(f"unknown validator keys {sorted(unknown)}")
        status = _text(body.get("status"), f"validator {name!r} requires a status")
        if status not in {"live", "pending_capture"}:
            raise OperationConfigError(f"validator {name!r} status {status!r} is invalid")
        pattern: re.Pattern[str] | None = None
        blocked_on: str | None = None
        if status == "live":
            raw_pattern = _text(body.get("pattern"), f"validator {name!r} requires a pattern")
            if body.get("blocked_on") is not None:
                raise OperationConfigError(f"live validator {name!r} must not carry a blocker")
            try:
                pattern = re.compile(raw_pattern)
            except re.error as error:
                raise OperationConfigError(
                    f"validator {name!r} pattern is invalid: {error}"
                ) from error
        else:
            if body.get("pattern") is not None:
                raise OperationConfigError(
                    f"pending validator {name!r} must not carry a pattern"
                )
            blocked_on = _text(
                body.get("blocked_on"), f"pending validator {name!r} requires a blocker"
            )
        parsed[name] = Validator(
            name=name, status=status, pattern=pattern, blocked_on=blocked_on
        )
    return parsed


def _parse_readback(raw: Any, validators: dict[str, Validator]) -> tuple[Readback, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise OperationConfigError("click path readback must be a list")
    dictionary = field_dictionary()
    captured: list[Readback] = []
    seen: set[str] = set()
    for entry in raw:
        values = _mapping(entry, "readback entry must be a mapping")
        unknown = set(values) - READBACK_KEYS
        if unknown:
            raise OperationConfigError(f"unknown readback keys {sorted(unknown)}")
        capture = _text(values.get("capture"), "readback capture is required")
        label = _text(values.get("label"), f"readback {capture!r} requires a label")
        into = _text(values.get("into"), f"readback {capture!r} requires an into path")
        if not into.startswith("external.") or into not in dictionary:
            raise OperationConfigError(
                f"readback {capture!r} target {into!r} is outside the external-field dictionary"
            )
        if into in seen:
            raise OperationConfigError(f"duplicate readback target {into!r}")
        seen.add(into)
        assert_format = _text(
            values.get("assert_format"), f"readback {capture!r} requires assert_format"
        )
        if assert_format not in validators:
            raise OperationConfigError(
                f"readback {capture!r} names undeclared validator {assert_format!r}"
            )
        required = values.get("required", True)
        if not isinstance(required, bool):
            raise OperationConfigError(f"readback {capture!r} required must be boolean")
        captured.append(
            Readback(
                capture=capture,
                label=label,
                into=into,
                assert_format=assert_format,
                required=required,
            )
        )
    return tuple(captured)


def load_click_path(path: Path, *, operation_id: str, version: str) -> ClickPath:
    """Parse and validate one versioned click path for a live catalogue row."""

    raw = _mapping(_load_yaml(path, "click path"), "click path must be a mapping")
    unknown = set(raw) - CLICK_PATH_KEYS
    if unknown:
        raise OperationConfigError(f"unknown click path keys {sorted(unknown)}")
    declared_operation = _text(raw.get("operation"), "click path operation is required")
    declared_version = _text(raw.get("version"), "click path version is required")
    if declared_operation != operation_id:
        raise OperationConfigError(
            f"click path declares operation {declared_operation!r}, not {operation_id!r}"
        )
    if declared_version != version:
        raise OperationConfigError(
            f"click path {operation_id!r} version {declared_version!r} does not match "
            f"the catalogue version {version!r}"
        )
    status = _text(raw.get("status"), "click path status is required")
    if status != "live":
        raise OperationConfigError(
            f"click path {operation_id!r} status {status!r} cannot back a live operation"
        )
    screens = _parse_screens(raw.get("screens"))
    steps = _parse_steps(raw.get("steps"), screens)
    validators = _parse_validators(raw.get("validators"))
    readback = _parse_readback(raw.get("readback"), validators)
    return ClickPath(
        operation=operation_id,
        version=version,
        status=status,
        screens=screens,
        steps=steps,
        readback=readback,
        validators=validators,
    )


class OperationRegistry:
    """The exact fifteen-row PRD-09 catalogue with resolved live click paths."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        catalogue = self.root / "operations.yaml"
        if not catalogue.is_file():
            raise OperationConfigError(f"operation catalogue {catalogue} does not exist")
        raw = _mapping(_load_yaml(catalogue, "operation catalogue"), "catalogue must be a mapping")
        unknown = set(raw) - ROOT_KEYS
        if unknown:
            raise OperationConfigError(f"unknown catalogue keys {sorted(unknown)}")
        if raw.get("version") != 1:
            raise OperationConfigError("operation catalogue requires version 1")
        self.sampling = _parse_sampling(raw.get("paste_readback_sampling"))
        self._operations = self._load_operations(raw.get("operations"))

    def _load_operations(self, raw: Any) -> dict[str, Operation]:
        if not isinstance(raw, list):
            raise OperationConfigError("catalogue operations must be a list")
        loaded: dict[str, Operation] = {}
        for entry in raw:
            values = _mapping(entry, "operation row must be a mapping")
            if set(values) != OPERATION_KEYS:
                raise OperationConfigError(
                    f"operation row keys are invalid: {sorted(values)}"
                )
            operation_id = _text(values["id"], "operation id is required")
            if operation_id not in OPERATION_IDS:
                raise OperationConfigError(f"unknown operation id {operation_id!r}")
            if operation_id in loaded:
                raise OperationConfigError(f"duplicate operation id {operation_id!r}")
            version = _text(values["version"], f"{operation_id} version is required")
            if SEMVER.fullmatch(version) is None:
                raise OperationConfigError(
                    f"{operation_id} version {version!r} is not major.minor.patch"
                )
            system = _text(values["system"], f"{operation_id} system is required")
            if system not in SYSTEMS:
                raise OperationConfigError(f"{operation_id} system {system!r} is unknown")
            if not operation_id.startswith(f"{system}."):
                raise OperationConfigError(f"{operation_id} does not belong to system {system!r}")
            mode = _text(values["mode"], f"{operation_id} mode is required")
            if mode not in MODES:
                raise OperationConfigError(f"{operation_id} mode {mode!r} is unsupported")
            status = _text(values["status"], f"{operation_id} status is required")
            if status not in AVAILABILITY:
                raise OperationConfigError(f"{operation_id} status {status!r} is unknown")
            owner_prd = _text(values["owner_prd"], f"{operation_id} owner_prd is required")
            if owner_prd not in OWNER_PRDS:
                raise OperationConfigError(f"{operation_id} owner_prd {owner_prd!r} is unknown")
            blocked_on = values["blocked_on"]
            click_path_ref = values["click_path_ref"]
            if status == "live":
                if blocked_on is not None:
                    raise OperationConfigError(f"{operation_id} is live but carries a blocker")
                if owner_prd != "PRD-09":
                    raise OperationConfigError(
                        f"{operation_id} is owned by {owner_prd} and cannot be live here"
                    )
                ref = _text(click_path_ref, f"{operation_id} is live without a click path")
                click_path = load_click_path(
                    _resolve_ref(self.root, ref),
                    operation_id=operation_id,
                    version=version,
                )
            else:
                if click_path_ref is not None:
                    raise OperationConfigError(
                        f"{operation_id} is not live but declares a click path"
                    )
                blocked_on = _text(blocked_on, f"{operation_id} is blocked without a blocker")
                click_path = None
            loaded[operation_id] = Operation(
                id=operation_id,
                version=version,
                system=system,
                mode=mode,
                status=status,
                blocked_on=None if status == "live" else blocked_on,
                click_path_ref=click_path_ref,
                owner_prd=owner_prd,
                click_path=click_path,
            )
        missing = sorted(set(OPERATION_IDS) - set(loaded))
        if missing:
            raise OperationConfigError(f"operation catalogue is missing {missing}")
        return {operation_id: loaded[operation_id] for operation_id in OPERATION_IDS}

    def __contains__(self, operation_id: object) -> bool:
        return operation_id in self._operations

    def get(self, operation_id: str) -> Operation:
        try:
            return self._operations[operation_id]
        except KeyError as error:
            raise OperationConfigError(f"unknown operation {operation_id!r}") from error

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(self._operations)

    def all(self) -> tuple[Operation, ...]:
        return tuple(self._operations.values())

    def catalogue(self) -> list[dict[str, Any]]:
        return [operation.catalogue_row() for operation in self._operations.values()]

    def capability_ids(self) -> tuple[str, ...]:
        return tuple(operation.capability_id for operation in self._operations.values())


__all__ = [
    "AVAILABILITY",
    "ClickPath",
    "OPERATION_IDS",
    "Operation",
    "OperationConfigError",
    "OperationRegistry",
    "Readback",
    "SamplingConfig",
    "Screen",
    "Step",
    "Validator",
    "load_click_path",
]
