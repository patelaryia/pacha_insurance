"""Fail-closed PRD-09 operation catalogue and click-path loader.

Registers #263/#265 (PACKET-20) and #281/#282/#285/#296 (PACKET-21).

The catalogue is pack data. Nothing here executes a browser action: PACKET-20
parsed the human-facing subset of the same versioned click-path definition, and
PACKET-21 extends the *same* YAML with the executable metadata — step effect,
timeout class, write id, postcondition, reconciliation mapping, prior-completion
probe, and captured failure signatures. There is still no second field-order or
browser registry.

The executable requirements are enforced only for a row that is actually
`mode=rpa, status=live`. A production row that is merely pending capture stays
visible and never fails startup.
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
        # PACKET-21 §5 executable additions.
        "reconciliation",
        "retry_probe",
        "known_failures",
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
        # PACKET-21 §5 executable additions.
        "effect",
        "timeout_class",
        "write_id",
        "postcondition",
    }
)
#: PACKET-21 §5. Every executable step declares exactly one effect class; the
#: declarations, not an operation-name heuristic, decide whether a definition is
#: read-only and therefore safe to re-lease after a stale lease.
EFFECTS = frozenset({"read_only", "local_input", "external_write"})
TIMEOUT_CLASSES = frozenset({"default", "edms", "upload"})
POSTCONDITION_KINDS = frozenset({"exact_value", "visible", "absent", "text_contains"})
POSTCONDITION_KEYS = frozenset({"kind", "selector", "value"})
PRECONDITION_KEYS = frozenset({"assert", "equals"})
#: Closed typed normalisers (register #285). No implicit trimming, case folding,
#: locale parsing, thousands separator, currency prefix, timezone conversion, or
#: rounding exists. A target that displays another representation needs a
#: captured, versioned normaliser before activation.
NORMALISERS = frozenset(
    {
        "string_exact",
        "enum_exact",
        "money_cents_exact",
        "money_shillings_to_cents_exact",
        "date_iso_exact",
        "datetime_iso_exact",
        "bool_exact",
    }
)
#: The one normaliser each captured target encoding may use. A definition that
#: names a different one is claiming a target representation nobody captured.
NORMALISER_BY_ENCODING: dict[tuple[str, str], str] = {
    ("string", "raw"): "string_exact",
    ("enum", "raw"): "enum_exact",
    ("money", "cents"): "money_cents_exact",
    ("money", "shillings"): "money_shillings_to_cents_exact",
    ("date", "iso"): "date_iso_exact",
    ("datetime", "iso"): "datetime_iso_exact",
}
RECONCILIATION_KEYS = frozenset({"step_id", "selector", "normaliser"})
NORMALISER_KEYS = frozenset({"kind"})
RETRY_PROBE_KEYS = frozenset({"keys", "exact_match", "absent", "ambiguous"})
PROBE_KEY_KEYS = frozenset({"from_step", "target"})
PROBE_EXACT_MATCH = "complete_without_write"
PROBE_ABSENT = "retry_only_if_no_external_write_completed"
PROBE_AMBIGUOUS = "uncertain_write"
KNOWN_FAILURE_KEYS = frozenset({"signature", "handler"})
#: Only the two PRD-09 §9.5 EDMS handlers exist. A third named handler is a
#: capture defect, never something to interpret.
KNOWN_FAILURE_HANDLERS = frozenset({"duplicate_filename", "slow_reflection"})
#: PRD-09 §9.4 fixes this literal. A definition that rewords it is rejected.
FAILURE_POLICY = "screenshot_always, halt_on_selector_miss, no_guessing"
PASTE_KEYS = frozenset({"label", "copy"})
READBACK_KEYS = frozenset(
    {"capture", "label", "into", "assert_format", "required", "selector"}
)
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
class Postcondition:
    """One captured assertion the runner makes after executing a step."""

    kind: str
    selector: str
    value: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "selector": self.selector, "value": self.value}


@dataclass(frozen=True)
class Precondition:
    """One captured assertion the runner makes before the first step."""

    assertion: str
    equals: str | None = None


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
    effect: str | None = None
    timeout_class: str | None = None
    write_id: str | None = None
    postcondition: Postcondition | None = None

    @property
    def is_copy_row(self) -> bool:
        return self.action in COPY_ACTIONS and self.value_kind is not None and self.copy

    @property
    def is_external_write(self) -> bool:
        return self.effect == "external_write"


@dataclass(frozen=True)
class Reconciliation:
    """One declared input whose target representation must be re-read exactly."""

    step_id: str
    selector: str
    normaliser: str


@dataclass(frozen=True)
class ProbeKey:
    """One deterministic key the prior-completion probe reads from the target."""

    from_step: str
    target: str


@dataclass(frozen=True)
class RetryProbe:
    """The captured prior-completion probe for a write-bearing definition."""

    keys: tuple[ProbeKey, ...]
    exact_match: str
    absent: str
    ambiguous: str


@dataclass(frozen=True)
class KnownFailure:
    """One captured target failure signature bound to a PRD-09 handler."""

    id: str
    signature: str
    handler: str


@dataclass(frozen=True)
class Readback:
    """One declared inline readback capture and its captured format validator.

    ``selector`` is the captured target element the value is read from. It has
    no meaning for paste-assist — an officer reads the screen — so it stays
    optional there and is mandatory for an executable RPA definition (#297).
    """

    capture: str
    label: str
    into: str
    assert_format: str
    required: bool
    selector: str | None = None


@dataclass(frozen=True)
class Validator:
    """A pack-declared readback format. `pending_capture` refuses every value."""

    name: str
    status: str
    pattern: re.Pattern[str] | None
    blocked_on: str | None


@dataclass(frozen=True)
class ClickPath:
    """One versioned operation definition: paste-facing and executable parts."""

    operation: str
    version: str
    status: str
    screens: tuple[Screen, ...]
    steps: tuple[Step, ...]
    readback: tuple[Readback, ...]
    validators: dict[str, Validator] = field(default_factory=dict)
    preconditions: tuple[Precondition, ...] = ()
    reconciliation: tuple[Reconciliation, ...] = ()
    retry_probe: RetryProbe | None = None
    known_failures: tuple[KnownFailure, ...] = ()
    failure_policy: str | None = None

    def steps_for(self, screen_id: str) -> tuple[Step, ...]:
        return tuple(step for step in self.steps if step.screen == screen_id)

    def step(self, step_id: str) -> Step:
        for step in self.steps:
            if step.id == step_id:
                return step
        raise OperationConfigError(f"unknown step {step_id!r}")

    @property
    def write_steps(self) -> tuple[Step, ...]:
        return tuple(step for step in self.steps if step.is_external_write)

    @property
    def is_read_only(self) -> bool:
        """A definition with zero `external_write` steps cannot mutate the target."""

        return not self.write_steps

    def handler_for(self, signature: str) -> str | None:
        for failure in self.known_failures:
            if failure.signature == signature:
                return failure.handler
        return None

    def as_definition(self) -> dict[str, Any]:
        """Serialise back to the exact YAML shape the loader parses.

        The control API hands this to an authorised runner so the runner parses
        the same versioned definition the platform validated — there is no
        second wire format and no second field-order registry.
        """

        return {
            "operation": self.operation,
            "version": self.version,
            "status": self.status,
            "preconditions": [
                {"assert": entry.assertion, "equals": entry.equals}
                for entry in self.preconditions
            ],
            "screens": [
                {"id": screen.id, "label": screen.label, "order": screen.order}
                for screen in self.screens
            ],
            "steps": [_step_definition(step) for step in self.steps],
            "readback": [
                {
                    "capture": entry.capture,
                    "label": entry.label,
                    "into": entry.into,
                    "assert_format": entry.assert_format,
                    "required": entry.required,
                    "selector": entry.selector,
                }
                for entry in self.readback
            ],
            "validators": {
                name: (
                    {"status": "live", "pattern": validator.pattern.pattern}
                    if validator.status == "live" and validator.pattern is not None
                    else {"status": validator.status, "blocked_on": validator.blocked_on}
                )
                for name, validator in self.validators.items()
            },
            "reconciliation": [
                {
                    "step_id": entry.step_id,
                    "selector": entry.selector,
                    "normaliser": {"kind": entry.normaliser},
                }
                for entry in self.reconciliation
            ],
            "retry_probe": (
                {}
                if self.retry_probe is None
                else {
                    "keys": [
                        {"from_step": key.from_step, "target": key.target}
                        for key in self.retry_probe.keys
                    ],
                    "exact_match": self.retry_probe.exact_match,
                    "absent": self.retry_probe.absent,
                    "ambiguous": self.retry_probe.ambiguous,
                }
            ),
            "known_failures": {
                failure.id: {"signature": failure.signature, "handler": failure.handler}
                for failure in self.known_failures
            },
            "failure_policy": self.failure_policy,
        }


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

    @property
    def is_live_rpa(self) -> bool:
        return self.status == "live" and self.mode == "rpa"

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
        effect = values.get("effect")
        if effect is not None and effect not in EFFECTS:
            raise OperationConfigError(f"step {step_id!r} effect {effect!r} is not declared")
        timeout_class = values.get("timeout_class")
        if timeout_class is not None and timeout_class not in TIMEOUT_CLASSES:
            raise OperationConfigError(
                f"step {step_id!r} timeout_class {timeout_class!r} is not declared"
            )
        write_id = values.get("write_id")
        if write_id is not None and (not isinstance(write_id, str) or not write_id.strip()):
            raise OperationConfigError(f"step {step_id!r} write_id must be a non-empty id")
        if write_id is not None and effect != "external_write":
            raise OperationConfigError(
                f"step {step_id!r} carries a write id without an external_write effect"
            )
        postcondition = _parse_postcondition(step_id, values.get("postcondition"))
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
                effect=effect,
                timeout_class=timeout_class,
                write_id=write_id,
                postcondition=postcondition,
            )
        )
    write_ids = [step.write_id for step in steps if step.write_id is not None]
    if len(set(write_ids)) != len(write_ids):
        raise OperationConfigError("click path repeats an external write id")
    return tuple(steps)


def _step_definition(step: Step) -> dict[str, Any]:
    """One step in the exact YAML shape, omitting bindings it does not declare."""

    definition: dict[str, Any] = {
        "id": step.id,
        "screen": step.screen,
        "action": step.action,
        "selector": step.selector,
        "effect": step.effect,
        "timeout_class": step.timeout_class,
        "write_id": step.write_id,
        "postcondition": (
            None if step.postcondition is None else step.postcondition.as_dict()
        ),
    }
    if step.value_kind == "field" and step.field_path is not None:
        definition["value"] = "{" + step.field_path + "}"
        definition["value_kind"] = "field"
    elif step.value_kind == "literal":
        definition["value"] = step.literal
        definition["value_kind"] = "literal"
    elif step.value_kind == "rule":
        definition["rule"] = step.rule_id
        definition["rule_values"] = dict(step.rule_values or {})
        definition["value_kind"] = "rule"
    if step.external_encoding is not None:
        definition["external_encoding"] = step.external_encoding
    if step.label is not None:
        definition["paste_assist"] = {"label": step.label, "copy": step.copy}
    return definition


def _parse_postcondition(step_id: str, raw: Any) -> Postcondition | None:
    if raw is None:
        return None
    values = _mapping(raw, f"step {step_id!r} postcondition must be a mapping")
    unknown = set(values) - POSTCONDITION_KEYS
    if unknown:
        raise OperationConfigError(f"unknown postcondition keys {sorted(unknown)}")
    kind = _text(values.get("kind"), f"step {step_id!r} postcondition requires a kind")
    if kind not in POSTCONDITION_KINDS:
        raise OperationConfigError(f"step {step_id!r} postcondition kind {kind!r} is unknown")
    selector = _text(
        values.get("selector"), f"step {step_id!r} postcondition requires a selector"
    )
    value = values.get("value")
    if value is not None and not isinstance(value, str):
        raise OperationConfigError(f"step {step_id!r} postcondition value must be a string")
    if kind == "text_contains" and value is None:
        raise OperationConfigError(
            f"step {step_id!r} text_contains postcondition requires a captured value"
        )
    return Postcondition(kind=kind, selector=selector, value=value)


def _parse_preconditions(raw: Any) -> tuple[Precondition, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise OperationConfigError("click path preconditions must be a list")
    parsed: list[Precondition] = []
    for entry in raw:
        values = _mapping(entry, "precondition must be a mapping")
        unknown = set(values) - PRECONDITION_KEYS
        if unknown:
            raise OperationConfigError(f"unknown precondition keys {sorted(unknown)}")
        assertion = _text(values.get("assert"), "precondition requires an assert")
        equals = values.get("equals")
        if equals is not None and not isinstance(equals, str):
            raise OperationConfigError("precondition equals must be a string")
        parsed.append(Precondition(assertion=assertion, equals=equals))
    return tuple(parsed)


def _parse_reconciliation(raw: Any) -> tuple[Reconciliation, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise OperationConfigError("click path reconciliation must be a list")
    parsed: list[Reconciliation] = []
    seen: set[str] = set()
    for entry in raw:
        values = _mapping(entry, "reconciliation entry must be a mapping")
        if set(values) != RECONCILIATION_KEYS:
            raise OperationConfigError(
                f"reconciliation entry keys are invalid: {sorted(values)}"
            )
        step_id = _text(values["step_id"], "reconciliation step_id is required")
        if step_id in seen:
            raise OperationConfigError(f"duplicate reconciliation entry for {step_id!r}")
        seen.add(step_id)
        selector = _text(values["selector"], f"reconciliation {step_id!r} needs a selector")
        normaliser = _mapping(
            values["normaliser"], f"reconciliation {step_id!r} needs a normaliser"
        )
        if set(normaliser) != NORMALISER_KEYS:
            raise OperationConfigError(
                f"reconciliation {step_id!r} normaliser keys are invalid"
            )
        kind = _text(normaliser["kind"], f"reconciliation {step_id!r} normaliser kind")
        if kind not in NORMALISERS:
            raise OperationConfigError(
                f"reconciliation {step_id!r} names undeclared normaliser {kind!r}"
            )
        parsed.append(Reconciliation(step_id=step_id, selector=selector, normaliser=kind))
    return tuple(parsed)


def _parse_retry_probe(raw: Any) -> RetryProbe | None:
    if raw is None or raw == {}:
        return None
    values = _mapping(raw, "retry_probe must be a mapping")
    if set(values) != RETRY_PROBE_KEYS:
        raise OperationConfigError(f"retry_probe keys are invalid: {sorted(values)}")
    raw_keys = values["keys"]
    if not isinstance(raw_keys, list) or not raw_keys:
        raise OperationConfigError("retry_probe requires at least one captured key")
    keys: list[ProbeKey] = []
    for entry in raw_keys:
        key_values = _mapping(entry, "retry_probe key must be a mapping")
        if set(key_values) != PROBE_KEY_KEYS:
            raise OperationConfigError("retry_probe key keys are invalid")
        keys.append(
            ProbeKey(
                from_step=_text(key_values["from_step"], "retry_probe from_step"),
                target=_text(key_values["target"], "retry_probe target"),
            )
        )
    # The three probe dispositions are fixed by PRD-09 §9.5; a definition that
    # rewrites one is claiming a recovery rule the source documents do not give.
    if (
        values["exact_match"] != PROBE_EXACT_MATCH
        or values["absent"] != PROBE_ABSENT
        or values["ambiguous"] != PROBE_AMBIGUOUS
    ):
        raise OperationConfigError("retry_probe dispositions are not the PRD-09 literals")
    return RetryProbe(
        keys=tuple(keys),
        exact_match=PROBE_EXACT_MATCH,
        absent=PROBE_ABSENT,
        ambiguous=PROBE_AMBIGUOUS,
    )


def _parse_known_failures(raw: Any) -> tuple[KnownFailure, ...]:
    if raw is None:
        return ()
    values = _mapping(raw, "known_failures must be a mapping")
    parsed: list[KnownFailure] = []
    seen_signatures: set[str] = set()
    for failure_id, entry in values.items():
        if not isinstance(failure_id, str) or not failure_id:
            raise OperationConfigError("known failure id must be a non-empty string")
        body = _mapping(entry, f"known failure {failure_id!r} must be a mapping")
        if set(body) != KNOWN_FAILURE_KEYS:
            raise OperationConfigError(f"known failure {failure_id!r} keys are invalid")
        signature = _text(body["signature"], f"known failure {failure_id!r} signature")
        if signature in seen_signatures:
            raise OperationConfigError(f"duplicate known failure signature {signature!r}")
        seen_signatures.add(signature)
        handler = _text(body["handler"], f"known failure {failure_id!r} handler")
        if handler not in KNOWN_FAILURE_HANDLERS:
            raise OperationConfigError(
                f"known failure {failure_id!r} names unimplemented handler {handler!r}"
            )
        parsed.append(
            KnownFailure(id=failure_id, signature=signature, handler=handler)
        )
    return tuple(parsed)


def assert_executable(click_path: ClickPath) -> None:
    """Refuse a live RPA definition that is missing any PACKET-21 §5 requirement."""

    if click_path.failure_policy != FAILURE_POLICY:
        raise OperationConfigError(
            f"{click_path.operation} failure_policy is not the PRD-09 literal"
        )
    if not click_path.preconditions:
        raise OperationConfigError(
            f"{click_path.operation} declares no captured preconditions"
        )
    for step in click_path.steps:
        if step.effect is None or step.timeout_class is None:
            raise OperationConfigError(
                f"{click_path.operation} step {step.id!r} does not declare its effect "
                "and timeout class"
            )
        if step.effect == "external_write":
            if not step.write_id:
                raise OperationConfigError(
                    f"{click_path.operation} step {step.id!r} writes without a write id"
                )
            if step.postcondition is None:
                raise OperationConfigError(
                    f"{click_path.operation} step {step.id!r} writes without a captured "
                    "postcondition"
                )
        elif step.write_id is not None:
            raise OperationConfigError(
                f"{click_path.operation} step {step.id!r} is not a write but carries a write id"
            )
    dictionary = field_dictionary()
    for entry in click_path.reconciliation:
        step = click_path.step(entry.step_id)
        if step.value_kind != "field" or step.field_path is None:
            required = "string_exact"
        else:
            value_type = dictionary[step.field_path].value_type
            required = NORMALISER_BY_ENCODING.get(
                (value_type, step.external_encoding or "raw"), ""
            )
        if entry.normaliser != required:
            raise OperationConfigError(
                f"{click_path.operation} step {entry.step_id!r} must reconcile with "
                f"{required!r}, not {entry.normaliser!r}"
            )
    reconciled = {entry.step_id for entry in click_path.reconciliation}
    payload_backed = {
        step.id
        for step in click_path.steps
        if step.action in COPY_ACTIONS and step.value_kind is not None
    }
    missing = sorted(payload_backed - reconciled)
    if missing:
        raise OperationConfigError(
            f"{click_path.operation} does not reconcile payload-backed steps {missing}"
        )
    unknown = sorted(reconciled - {step.id for step in click_path.steps})
    if unknown:
        raise OperationConfigError(
            f"{click_path.operation} reconciles unknown steps {unknown}"
        )
    if not click_path.readback:
        raise OperationConfigError(
            f"{click_path.operation} declares no target output to read back"
        )
    for entry in click_path.readback:
        if entry.selector is None:
            raise OperationConfigError(
                f"{click_path.operation} readback {entry.capture!r} has no captured selector"
            )
    if click_path.write_steps:
        probe = click_path.retry_probe
        if probe is None:
            raise OperationConfigError(
                f"{click_path.operation} writes without a captured prior-completion probe"
            )
        step_ids = {step.id for step in click_path.steps}
        for key in probe.keys:
            if key.from_step not in step_ids:
                raise OperationConfigError(
                    f"{click_path.operation} probe names unknown step {key.from_step!r}"
                )
    elif click_path.retry_probe is not None:
        raise OperationConfigError(
            f"{click_path.operation} is read-only but declares a retry probe"
        )


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
        selector = values.get("selector")
        if selector is not None and (not isinstance(selector, str) or not selector.strip()):
            raise OperationConfigError(f"readback {capture!r} selector must be a selector")
        captured.append(
            Readback(
                capture=capture,
                label=label,
                into=into,
                assert_format=assert_format,
                required=required,
                selector=selector,
            )
        )
    return tuple(captured)


def load_click_path(
    path: Path, *, operation_id: str, version: str, executable: bool = False
) -> ClickPath:
    """Parse and validate one versioned click path for a live catalogue row.

    ``executable`` adds the PACKET-21 §5 requirements a `mode=rpa` row must meet
    before it may ever be handed to a runner.
    """

    return parse_click_path(
        _load_yaml(path, "click path"),
        operation_id=operation_id,
        version=version,
        executable=executable,
    )


def parse_click_path(
    raw_definition: Any, *, operation_id: str, version: str, executable: bool = False
) -> ClickPath:
    """Parse one already-loaded click-path mapping through the same validation."""

    raw = _mapping(raw_definition, "click path must be a mapping")
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
    failure_policy = raw.get("failure_policy")
    if failure_policy is not None and not isinstance(failure_policy, str):
        raise OperationConfigError("click path failure_policy must be a string")
    click_path = ClickPath(
        operation=operation_id,
        version=version,
        status=status,
        screens=screens,
        steps=steps,
        readback=readback,
        validators=validators,
        preconditions=_parse_preconditions(raw.get("preconditions")),
        reconciliation=_parse_reconciliation(raw.get("reconciliation")),
        retry_probe=_parse_retry_probe(raw.get("retry_probe")),
        known_failures=_parse_known_failures(raw.get("known_failures")),
        failure_policy=failure_policy,
    )
    if executable:
        assert_executable(click_path)
    return click_path


#: PACKET-21 §4. The PRD-09 §9.4 step ceilings are binding, so a pack may lower
#: a class but never raise it above the captured PRD value.
TIMEOUT_CEILINGS = {
    "default_step_timeout_seconds": 20,
    "edms_step_timeout_seconds": 90,
    "upload_timeout_seconds": 480,
    "reflection_poll_seconds": 30,
    "reflection_timeout_seconds": 600,
}
RUNNER_KEYS = frozenset(
    {
        "heartbeat_seconds",
        "lease_seconds",
        "reaper_seconds",
        "max_attempts",
        *TIMEOUT_CEILINGS,
        "screenshot_policy",
        "session_policy",
    }
)
RUNTIME_ROOT_KEYS = frozenset({"version", "runner", "control_api_auth", "drift"})
AUTH_KEYS = frozenset({"status", "blocked_on"})
AUTH_STATUSES = frozenset({"live", "blocked_on_inputs"})
DRIFT_SCHEDULE_STATUSES = frozenset({"live", "pending_capture"})
#: AR-1 fixes the safe-attempt ceiling at three.
MAX_ATTEMPT_CEILING = 3
SCREENSHOT_POLICY = "before_and_after_every_step"
SESSION_POLICY = "isolated_context_per_run"

DRIFT_ROOT_KEYS = frozenset({"version", "status", "blocked_on", "schedule", "checks"})
DRIFT_SCHEDULE_KEYS = frozenset({"day_of_week", "hour", "minute", "timezone"})
DRIFT_CHECK_KEYS = frozenset(
    {
        "id",
        "status",
        "blocked_on",
        "source_operation",
        "external_ref",
        "claim_path",
        "target_readback",
    }
)


@dataclass(frozen=True)
class RunnerTimings:
    """Pack-owned runner control values. Code holds no fallback literal."""

    heartbeat_seconds: int
    lease_seconds: int
    reaper_seconds: int
    max_attempts: int
    default_step_timeout_seconds: int
    edms_step_timeout_seconds: int
    upload_timeout_seconds: int
    reflection_poll_seconds: int
    reflection_timeout_seconds: int
    screenshot_policy: str
    session_policy: str

    def step_timeout(self, timeout_class: str) -> int:
        if timeout_class == "edms":
            return self.edms_step_timeout_seconds
        if timeout_class == "upload":
            return self.upload_timeout_seconds
        return self.default_step_timeout_seconds


@dataclass(frozen=True)
class RuntimeConfig:
    """The parsed `runtime.yaml` control plane configuration."""

    runner: RunnerTimings
    control_api_auth_status: str
    control_api_blocked_on: str | None
    drift_schedule_status: str
    drift_blocked_on: str | None


@dataclass(frozen=True)
class DriftCheck:
    """One registered nightly reconciliation between platform and target."""

    id: str
    status: str
    blocked_on: str | None
    source_operation: str
    external_ref: str
    claim_path: str | None
    target_readback: str | None

    @property
    def is_live(self) -> bool:
        return self.status == "live"


@dataclass(frozen=True)
class DriftConfig:
    """The parsed `drift.yaml` standing registry."""

    status: str
    blocked_on: str | None
    day_of_week: str | None
    hour: int | None
    minute: int | None
    timezone: str
    checks: tuple[DriftCheck, ...]

    @property
    def live_checks(self) -> tuple[DriftCheck, ...]:
        return tuple(check for check in self.checks if check.is_live)

    @property
    def schedulable(self) -> bool:
        return (
            self.status == "live"
            and self.hour is not None
            and self.minute is not None
            and self.day_of_week is not None
        )


def _positive_int(values: dict[str, Any], key: str) -> int:
    value = _int(values.get(key), f"runtime runner {key} must be an integer")
    if value <= 0:
        raise OperationConfigError(f"runtime runner {key} must be positive")
    return value


def load_runtime_config(path: Path) -> RuntimeConfig:
    """Parse and validate `runtime.yaml`, failing closed on any invalid value."""

    raw = _mapping(_load_yaml(path, "projection runtime"), "runtime config must be a mapping")
    if set(raw) != RUNTIME_ROOT_KEYS:
        raise OperationConfigError(f"runtime config keys are invalid: {sorted(raw)}")
    if raw.get("version") != 1:
        raise OperationConfigError("runtime config requires version 1")
    runner = _mapping(raw.get("runner"), "runtime runner must be a mapping")
    if set(runner) != RUNNER_KEYS:
        raise OperationConfigError(f"runtime runner keys are invalid: {sorted(runner)}")
    timings = {key: _positive_int(runner, key) for key in sorted(RUNNER_KEYS - {
        "screenshot_policy",
        "session_policy",
    })}
    for key, ceiling in TIMEOUT_CEILINGS.items():
        if timings[key] > ceiling:
            raise OperationConfigError(
                f"runtime runner {key} exceeds the captured PRD-09 ceiling of {ceiling}s"
            )
    if timings["max_attempts"] != MAX_ATTEMPT_CEILING:
        raise OperationConfigError(
            f"runtime runner max_attempts must be the AR-1 ceiling {MAX_ATTEMPT_CEILING}"
        )
    if timings["heartbeat_seconds"] >= timings["lease_seconds"]:
        raise OperationConfigError("runtime heartbeat must be shorter than the lease")
    if timings["lease_seconds"] < 2 * timings["heartbeat_seconds"]:
        raise OperationConfigError("runtime lease must be at least two heartbeats")
    if timings["reaper_seconds"] > timings["lease_seconds"]:
        raise OperationConfigError("runtime reaper must not run less often than the lease")
    if runner.get("screenshot_policy") != SCREENSHOT_POLICY:
        raise OperationConfigError("runtime screenshot_policy is not the binding value")
    if runner.get("session_policy") != SESSION_POLICY:
        raise OperationConfigError("runtime session_policy is not the binding value")

    auth = _mapping(raw.get("control_api_auth"), "control_api_auth must be a mapping")
    if set(auth) != AUTH_KEYS:
        raise OperationConfigError("control_api_auth keys are invalid")
    auth_status = _text(auth.get("status"), "control_api_auth status is required")
    if auth_status not in AUTH_STATUSES:
        raise OperationConfigError(f"control_api_auth status {auth_status!r} is unknown")
    blocked_on = auth.get("blocked_on")
    if auth_status == "blocked_on_inputs":
        blocked_on = _text(blocked_on, "a blocked control_api_auth needs a blocker")
    elif blocked_on is not None:
        raise OperationConfigError("a live control_api_auth must not carry a blocker")

    drift = _mapping(raw.get("drift"), "runtime drift must be a mapping")
    if set(drift) != {"schedule"}:
        raise OperationConfigError("runtime drift keys are invalid")
    schedule = _mapping(drift.get("schedule"), "runtime drift schedule must be a mapping")
    if set(schedule) != AUTH_KEYS:
        raise OperationConfigError("runtime drift schedule keys are invalid")
    drift_status = _text(schedule.get("status"), "drift schedule status is required")
    if drift_status not in DRIFT_SCHEDULE_STATUSES:
        raise OperationConfigError(f"drift schedule status {drift_status!r} is unknown")
    drift_blocked_on = schedule.get("blocked_on")
    if drift_status == "pending_capture":
        drift_blocked_on = _text(drift_blocked_on, "a pending drift schedule needs a blocker")
    elif drift_blocked_on is not None:
        raise OperationConfigError("a live drift schedule must not carry a blocker")

    return RuntimeConfig(
        runner=RunnerTimings(
            **{key: timings[key] for key in timings},
            screenshot_policy=SCREENSHOT_POLICY,
            session_policy=SESSION_POLICY,
        ),
        control_api_auth_status=auth_status,
        control_api_blocked_on=blocked_on,
        drift_schedule_status=drift_status,
        drift_blocked_on=drift_blocked_on,
    )


def load_drift_config(path: Path) -> DriftConfig:
    """Parse and validate `drift.yaml`. A pending row keeps its blocker visible."""

    raw = _mapping(_load_yaml(path, "projection drift"), "drift config must be a mapping")
    if set(raw) != DRIFT_ROOT_KEYS:
        raise OperationConfigError(f"drift config keys are invalid: {sorted(raw)}")
    if raw.get("version") != 1:
        raise OperationConfigError("drift config requires version 1")
    status = _text(raw.get("status"), "drift status is required")
    if status not in {"live", "pending_capture"}:
        raise OperationConfigError(f"drift status {status!r} is unknown")
    blocked_on = raw.get("blocked_on")
    if status == "pending_capture":
        blocked_on = _text(blocked_on, "a pending drift registry needs a blocker")
    elif blocked_on is not None:
        raise OperationConfigError("a live drift registry must not carry a blocker")
    schedule = _mapping(raw.get("schedule"), "drift schedule must be a mapping")
    if set(schedule) != DRIFT_SCHEDULE_KEYS:
        raise OperationConfigError("drift schedule keys are invalid")
    timezone = _text(schedule.get("timezone"), "drift schedule timezone is required")
    try:
        ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise OperationConfigError(
            f"drift schedule timezone {timezone!r} is not registered"
        ) from error
    hour = schedule.get("hour")
    minute = schedule.get("minute")
    day_of_week = schedule.get("day_of_week")
    if status == "live":
        hour = _int(hour, "a live drift schedule requires an hour")
        minute = _int(minute, "a live drift schedule requires a minute")
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise OperationConfigError("drift schedule hour/minute are out of range")
        day_of_week = _text(day_of_week, "a live drift schedule requires a day_of_week")
    else:
        if hour is not None or minute is not None:
            raise OperationConfigError(
                "a pending drift schedule must not carry a captured time"
            )
        if day_of_week is not None and not isinstance(day_of_week, str):
            raise OperationConfigError("drift schedule day_of_week must be a string")
    raw_checks = raw.get("checks")
    if not isinstance(raw_checks, list) or not raw_checks:
        raise OperationConfigError("drift config requires a non-empty checks list")
    checks: list[DriftCheck] = []
    seen: set[str] = set()
    for entry in raw_checks:
        values = _mapping(entry, "drift check must be a mapping")
        if set(values) != DRIFT_CHECK_KEYS:
            raise OperationConfigError(f"drift check keys are invalid: {sorted(values)}")
        check_id = _text(values["id"], "drift check id is required")
        if check_id in seen:
            raise OperationConfigError(f"duplicate drift check {check_id!r}")
        seen.add(check_id)
        check_status = _text(values["status"], f"drift check {check_id!r} status")
        if check_status not in {"live", "pending_capture"}:
            raise OperationConfigError(f"drift check {check_id!r} status is unknown")
        check_blocked = values["blocked_on"]
        claim_path = values["claim_path"]
        target_readback = values["target_readback"]
        if check_status == "pending_capture":
            check_blocked = _text(check_blocked, f"drift check {check_id!r} needs a blocker")
        else:
            if check_blocked is not None:
                raise OperationConfigError(f"live drift check {check_id!r} carries a blocker")
            claim_path = _text(claim_path, f"live drift check {check_id!r} needs a claim path")
            target_readback = _text(
                target_readback, f"live drift check {check_id!r} needs a target readback"
            )
        source_operation = _text(
            values["source_operation"], f"drift check {check_id!r} source_operation"
        )
        external_ref = _text(values["external_ref"], f"drift check {check_id!r} external_ref")
        checks.append(
            DriftCheck(
                id=check_id,
                status=check_status,
                blocked_on=check_blocked,
                source_operation=source_operation,
                external_ref=external_ref,
                claim_path=claim_path if isinstance(claim_path, str) else None,
                target_readback=(
                    target_readback if isinstance(target_readback, str) else None
                ),
            )
        )
    return DriftConfig(
        status=status,
        blocked_on=blocked_on,
        day_of_week=day_of_week if isinstance(day_of_week, str) else None,
        hour=hour if isinstance(hour, int) and not isinstance(hour, bool) else None,
        minute=minute if isinstance(minute, int) and not isinstance(minute, bool) else None,
        timezone=timezone,
        checks=tuple(checks),
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
        self.runtime = load_runtime_config(self.root / "runtime.yaml")
        self.drift = load_drift_config(self.root / "drift.yaml")
        if self.drift.status == "live" and any(
            check.is_live and check.target_readback is None for check in self.drift.checks
        ):
            raise OperationConfigError(
                "a live drift schedule requires a complete readback registry"
            )

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
                if mode == "api":
                    # PRD-09 registers the `api` mode but PACKET-21 implements no
                    # API executor, so a live `api` row would be an unbacked promise.
                    raise OperationConfigError(
                        f"{operation_id} declares api mode, which has no registered executor"
                    )
                click_path = load_click_path(
                    _resolve_ref(self.root, ref),
                    operation_id=operation_id,
                    version=version,
                    executable=mode == "rpa",
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
    "DriftCheck",
    "DriftConfig",
    "EFFECTS",
    "FAILURE_POLICY",
    "KNOWN_FAILURE_HANDLERS",
    "KnownFailure",
    "NORMALISERS",
    "OPERATION_IDS",
    "Operation",
    "OperationConfigError",
    "OperationRegistry",
    "Postcondition",
    "Precondition",
    "ProbeKey",
    "Readback",
    "Reconciliation",
    "RetryProbe",
    "RunnerTimings",
    "RuntimeConfig",
    "SamplingConfig",
    "Screen",
    "Step",
    "TIMEOUT_CEILINGS",
    "Validator",
    "assert_executable",
    "load_click_path",
    "parse_click_path",
    "load_drift_config",
    "load_runtime_config",
]
