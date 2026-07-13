"""Fail-closed pack template registry and rendering engine."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, StrictUndefined, TemplateError
from sqlalchemy.orm import sessionmaker

from claim_core import new_ulid
from cop_runtime.errors import PackLoadError

VERIFICATION_RANK = {"extracted": 0, "system_confirmed": 1, "human_verified": 2}
TEMPLATE_ID = re.compile(r"^T-[A-Za-z0-9-]+$")
CONTEXT_VARIABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SEMVER = re.compile(r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$")


@dataclass(frozen=True)
class TemplateDefinition:
    """One validated template registry row."""

    template_id: str
    version: str
    channel: str
    body_ref: str | None
    required_fields: tuple[str, ...]
    min_verification: str
    locale: str
    status: str
    calc_slots: dict[str, str]
    blocked_on: tuple[str, ...]
    body_path: Path | None


class TemplateRegistry:
    """Read-only template definitions for one pack version."""

    def __init__(self, definitions: dict[str, TemplateDefinition]) -> None:
        self._definitions = dict(definitions)

    def ids(self) -> list[str]:
        return list(self._definitions)

    def get(self, template_id: str) -> TemplateDefinition:
        try:
            return self._definitions[template_id]
        except KeyError as error:
            raise LookupError(f"Unknown template id {template_id!r}") from error


@dataclass(frozen=True)
class RenderResult:
    """Metadata for one immutable rendered artifact."""

    template_id: str
    template_version: str
    pack_id: str
    pack_version: str
    channel: str
    blob_key: str
    signable: bool
    placeholders_pending: list[str]


class TemplateRenderBlocked(RuntimeError):
    """A template refused to render because required integrity inputs were absent."""

    def __init__(
        self,
        *,
        reason: str,
        missing_fields: list[str] | None = None,
        under_verified: list[str] | None = None,
    ) -> None:
        self.reason = reason
        self.missing_fields = list(missing_fields or [])
        self.under_verified = list(under_verified or [])
        super().__init__(reason)


def money_kes_display(value: int) -> str:
    """Format integer KES cents for display without round-tripping through floats."""

    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("money_kes_display requires integer KES cents")
    shillings, cents = divmod(abs(value), 100)
    sign = "-" if value < 0 else ""
    return f"KES {sign}{shillings:,}.{cents:02d}"


def _blocked_on(raw: Any, *, template_id: str) -> tuple[str, ...]:
    if isinstance(raw, str) and raw.strip():
        return (raw,)
    if isinstance(raw, list) and raw and all(
        isinstance(item, str) and item.strip() for item in raw
    ):
        return tuple(raw)
    raise PackLoadError(f"Pending template {template_id} requires blocked_on")


def load_template_registry(
    path: Path,
    *,
    calc_ids: set[str],
    known_fields: set[str],
) -> TemplateRegistry:
    """Load a registry after validating bodies, calc references, and closed metadata."""

    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise PackLoadError(f"Invalid template registry {path}: {error}") from error
    if not isinstance(payload, dict) or set(payload) != {"templates"}:
        raise PackLoadError("Template registry must contain only a templates list")
    rows = payload["templates"]
    if not isinstance(rows, list):
        raise PackLoadError("Template registry templates must be a list")

    definitions: dict[str, TemplateDefinition] = {}
    templates_root = path.parent.resolve()
    allowed_keys = {
        "id",
        "version",
        "channel",
        "body_ref",
        "required_fields",
        "min_verification",
        "locale",
        "status",
        "calc_slots",
        "blocked_on",
    }
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not set(row).issubset(allowed_keys):
            raise PackLoadError(f"Template registry row {index} has invalid keys")
        required_keys = {
            "id",
            "version",
            "channel",
            "body_ref",
            "required_fields",
            "min_verification",
            "status",
        }
        if not required_keys.issubset(row):
            raise PackLoadError(f"Template registry row {index} is incomplete")
        template_id = row["id"]
        if not isinstance(template_id, str) or not TEMPLATE_ID.fullmatch(template_id):
            raise PackLoadError(f"Template registry row {index} has invalid id")
        if template_id in definitions:
            raise PackLoadError(f"Duplicate template id {template_id!r}")
        version = row["version"]
        if not isinstance(version, str) or not SEMVER.fullmatch(version):
            raise PackLoadError(f"Template {template_id} has invalid version")
        channel = row["channel"]
        if channel not in {"email", "pdf", "field_set"}:
            raise PackLoadError(f"Template {template_id} has invalid channel")
        required_fields = row["required_fields"]
        if not isinstance(required_fields, list) or not all(
            isinstance(item, str) and item.strip() for item in required_fields
        ):
            raise PackLoadError(f"Template {template_id} has invalid required_fields")
        if len(required_fields) != len(set(required_fields)):
            raise PackLoadError(f"Template {template_id} repeats a required field")
        unknown_fields = sorted(set(required_fields) - known_fields)
        if unknown_fields:
            raise PackLoadError(
                f"Template {template_id} references unknown fields {unknown_fields}"
            )
        minimum = row["min_verification"]
        if minimum not in {"extracted", "human_verified"}:
            raise PackLoadError(f"Template {template_id} has invalid min_verification")
        locale = row.get("locale", "en-KE")
        if locale != "en-KE":
            raise PackLoadError(f"Template {template_id} has unsupported locale")
        status = row["status"]
        if status not in {"live", "pending_capture"}:
            raise PackLoadError(f"Template {template_id} has invalid status")
        if status == "pending_capture":
            blocked_on = _blocked_on(row.get("blocked_on"), template_id=template_id)
        elif "blocked_on" in row:
            raise PackLoadError(f"Live template {template_id} must not declare blocked_on")
        else:
            blocked_on = ()

        calc_slots = row.get("calc_slots", {})
        if not isinstance(calc_slots, dict) or not all(
            isinstance(variable, str)
            and CONTEXT_VARIABLE.fullmatch(variable)
            and isinstance(calc_id, str)
            for variable, calc_id in calc_slots.items()
        ):
            raise PackLoadError(f"Template {template_id} has invalid calc_slots")
        dangling = sorted(set(calc_slots.values()) - calc_ids)
        if dangling:
            raise PackLoadError(
                f"Template {template_id} references unknown calculations {dangling}"
            )

        aliases = [field.replace(".", "_") for field in required_fields]
        if len(aliases) != len(set(aliases)) or set(aliases) & set(calc_slots):
            raise PackLoadError(f"Template {template_id} has colliding context variables")
        body_ref = row["body_ref"]
        body_path: Path | None = None
        if channel == "field_set":
            if body_ref is not None:
                raise PackLoadError(f"Field-set template {template_id} must not have a body")
        elif status == "live":
            if not isinstance(body_ref, str) or not body_ref.strip():
                raise PackLoadError(f"Live template {template_id} requires body_ref")
            candidate = (templates_root / body_ref).resolve()
            if templates_root not in candidate.parents or not candidate.is_file():
                raise PackLoadError(f"Template {template_id} body_ref does not exist")
            body_path = candidate
        elif body_ref is not None and not isinstance(body_ref, str):
            raise PackLoadError(f"Template {template_id} has invalid body_ref")

        definitions[template_id] = TemplateDefinition(
            template_id=template_id,
            version=version,
            channel=channel,
            body_ref=body_ref,
            required_fields=tuple(required_fields),
            min_verification=minimum,
            locale=locale,
            status=status,
            calc_slots=dict(calc_slots),
            blocked_on=blocked_on,
            body_path=body_path,
        )
    return TemplateRegistry(definitions)


class TemplateEngine:
    """Render validated definitions to the configured immutable blob store."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self._claim_service = runtime._app.state.claim_service
        self._blob_store = runtime._app.state.blob_store
        self._sessions = sessionmaker(
            bind=runtime._app.state.engine,
            expire_on_commit=False,
        )
        self._environment = Environment(
            undefined=StrictUndefined,
            autoescape=False,
            keep_trailing_newline=True,
        )
        self._environment.filters = {"money_kes_display": money_kes_display}

    def _artifact(
        self,
        definition: TemplateDefinition,
        *,
        context: dict[str, Any],
        fields: dict[str, Any],
    ) -> bytes:
        try:
            if definition.channel == "field_set":
                return json.dumps(
                    {path: fields[path].value for path in definition.required_fields},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            if definition.body_path is None:
                raise RuntimeError("Validated live text template has no body")
            source = definition.body_path.read_text(encoding="utf-8")
            return self._environment.from_string(source).render(context).encode()
        except (OSError, TemplateError, TypeError) as error:
            raise TemplateRenderBlocked(
                reason="missing_fields",
                missing_fields=[str(error)],
            ) from error

    def render(
        self,
        definition: TemplateDefinition,
        *,
        pack: Any,
        claim_id: str,
        actor: str,
    ) -> RenderResult:
        """Render one definition or refuse with an explicit integrity reason."""

        if definition.status == "pending_capture":
            raise TemplateRenderBlocked(reason="pending_capture")
        _claim, fields, _blocked = self._claim_service.hydrate_claim(
            claim_id,
            actor,
            paths=definition.required_fields,
        )
        missing = [path for path in definition.required_fields if path not in fields]
        if missing:
            raise TemplateRenderBlocked(reason="missing_fields", missing_fields=missing)
        under_verified = [
            path
            for path in definition.required_fields
            if VERIFICATION_RANK.get(fields[path].verification_state, -1)
            < VERIFICATION_RANK[definition.min_verification]
        ]
        if under_verified:
            raise TemplateRenderBlocked(
                reason="under_verified",
                under_verified=under_verified,
            )

        context = {
            path.replace(".", "_"): fields[path].value
            for path in definition.required_fields
        }
        placeholders_pending: list[str] = []
        with self._sessions.begin() as session:
            for variable, calc_id in definition.calc_slots.items():
                result = self._runtime.execute_calc(
                    calc_id,
                    claim_id,
                    actor,
                    _session=session,
                )
                if result.status == "blocked_on_inputs":
                    context[variable] = "PENDING CAPTURE"
                    placeholders_pending.append(variable)
                elif result.status == "executed":
                    context[variable] = result.output
                else:
                    raise RuntimeError(
                        f"Unknown calculation result status {result.status!r}"
                    )

            artifact = self._artifact(definition, context=context, fields=fields)
            blob_key = f"templates/{claim_id}/{definition.template_id}/{new_ulid()}"
            self._blob_store.put(blob_key, artifact)
            signable = not placeholders_pending
            self._claim_service.record_event(
                session,
                claim_id=claim_id,
                event_type="template.rendered",
                payload={
                    "template_id": definition.template_id,
                    "template_version": definition.version,
                    "pack": f"{pack.pack_id}@{pack.version}",
                    "channel": definition.channel,
                    "blob_key": blob_key,
                    "signable": signable,
                },
                actor=actor,
                correlation_id=None,
            )
        return RenderResult(
            template_id=definition.template_id,
            template_version=definition.version,
            pack_id=pack.pack_id,
            pack_version=pack.version,
            channel=definition.channel,
            blob_key=blob_key,
            signable=signable,
            placeholders_pending=placeholders_pending,
        )


__all__ = [
    "RenderResult",
    "TemplateDefinition",
    "TemplateEngine",
    "TemplateRegistry",
    "TemplateRenderBlocked",
    "load_template_registry",
    "money_kes_display",
]
