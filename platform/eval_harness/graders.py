"""Deterministic PRD-03 graders and the closed nine-grader registry."""

from __future__ import annotations

import hashlib
import io
import json
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from json_logic import jsonLogic
from pypdf import PdfReader
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from doc_intel.validators import money_kes, validate_field
from doc_intel.vision import crop_png, normalized_bbox
from eval_harness.models import GraderRun

GradeOutcome = Literal["pass", "fail", "error"]


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _decoded_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


@dataclass(frozen=True)
class GraderResult:
    """Public immutable grade result."""

    grader_id: str
    subject_type: str
    result: GradeOutcome
    severity: str
    detail: dict[str, Any]
    grader_run_id: str


@dataclass(frozen=True)
class RawGrade:
    """A grader's result before the harness records it."""

    result: GradeOutcome
    detail: dict[str, Any]


class Grader:
    """Base metadata and deterministic grade contract."""

    def __init__(
        self,
        harness: Any,
        grader_id: str,
        subject_type: str,
        severity: str,
        *,
        status: str = "live",
        blocked_on: str | None = None,
    ) -> None:
        self.harness = harness
        self.grader_id = grader_id
        self.subject_type = subject_type
        self.severity = severity
        self.status = status
        self.blocked_on = blocked_on

    def grade(self, subject: dict[str, Any], actor: str) -> RawGrade:
        raise NotImplementedError


class PendingGrader(Grader):
    """Visible slot for a grader whose producing dependency is not in scope."""

    def grade(self, subject: dict[str, Any], actor: str) -> RawGrade:
        del subject, actor
        return RawGrade("error", {"code": "GRADER_PENDING", "blocked_on": self.blocked_on})


class ValueGrader(Grader):
    """Re-run the schema-named validator against the current canonical value."""

    def __init__(self, harness: Any) -> None:
        super().__init__(harness, "G-VAL", "field", "critical")
        self._validators = self._target_validators()
        # PRD-09 readback formats are captured configuration, not doc-intel
        # schema targets, so their owner registers them at build time. No tenth
        # grader id is invented: the same critical G-VAL grades them.
        self._external: dict[str, tuple[str, Callable[[Any], bool]]] = {}

    def register_external_validator(
        self, path: str, *, name: str, check: Callable[[Any], bool]
    ) -> None:
        """Register one captured validator for an external readback field path."""

        registered = self._external.get(path)
        if registered is not None and registered[0] != name:
            raise ValueError(f"conflicting external validators for {path!r}")
        self._external[path] = (name, check)

    @staticmethod
    def _target_validators() -> dict[str, str]:
        schemas = Path(__file__).resolve().parents[1] / "doc_intel" / "schemas" / "motor"
        mappings: dict[str, str] = {}
        for path in sorted(schemas.glob("*.yaml")):
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
            for definition in payload.get("fields", {}).values():
                target = definition.get("target_path")
                validator = definition.get("validator")
                if isinstance(target, str) and isinstance(validator, str):
                    prior = mappings.get(target)
                    if prior is not None and prior != validator:
                        raise ValueError(f"conflicting validators for {target!r}")
                    mappings[target] = validator
        return mappings

    def grade(self, subject: dict[str, Any], actor: str) -> RawGrade:
        claim_id = subject.get("claim_id")
        path = subject.get("path")
        if not isinstance(claim_id, str) or not isinstance(path, str):
            return RawGrade("error", {"code": "INVALID_SUBJECT_REF"})
        external = self._external.get(path)
        validator = self._validators.get(path)
        if validator is None and external is None:
            return RawGrade("error", {"code": "NO_SCHEMA_TARGET_MAPPING", "path": path})
        _claim, fields, _blocked = self.harness.claim_service.hydrate_claim(
            claim_id,
            actor,
            paths=[path],
        )
        field = fields.get(path)
        if field is None:
            return RawGrade("error", {"code": "CURRENT_FIELD_MISSING", "path": path})
        if external is not None:
            name, check = external
            passed = check(field.value)
            return RawGrade(
                "pass" if passed else "fail",
                {"validator": name, "outcome": "pass" if passed else "fail"},
            )
        outcome = validate_field(validator, field.value, today=self.harness.clock().date())
        result: GradeOutcome = "pass" if outcome.outcome in {"pass", "not_applicable"} else "fail"
        return RawGrade(result, {"validator": validator, "outcome": outcome.outcome})


GCITE_SCHEMA = {
    "type": "object",
    "required": ["value_present", "observed_value"],
    "additionalProperties": False,
    "properties": {
        "value_present": {"type": "boolean"},
        "observed_value": {"type": ["string", "integer", "null"]},
    },
}


class CitationGrader(Grader):
    """Verify a current extracted value against only its cited render crop."""

    def __init__(self, harness: Any) -> None:
        super().__init__(harness, "G-CITE", "field", "critical")

    def _field(self, claim_id: str, path: str, actor: str) -> dict[str, Any] | None:
        try:
            _claim, fields, _blocked = self.harness.claim_service.hydrate_claim(
                claim_id,
                actor,
                paths=[path],
            )
        except Exception:  # noqa: BLE001 - missing/inaccessible current data is invalid
            return None
        field = fields.get(path)
        if field is None:
            return None
        return {
            "id": field.id,
            "value": field.value,
            "value_type": field.value_type,
            "source_type": field.source_type,
            "source_ref": field.source_ref,
            "verification_state": field.verification_state,
        }

    @staticmethod
    def _exact_numeric(value_type: str, observed: Any, current: Any) -> bool:
        if value_type == "money":
            parsed = money_kes(observed)
            return (
                parsed.outcome == "pass"
                and isinstance(current, int)
                and not isinstance(current, bool)
                and parsed.value == current
            )
        if value_type == "date":
            if not isinstance(observed, str) or not isinstance(current, str):
                return False
            try:
                return date.fromisoformat(observed) == date.fromisoformat(current)
            except ValueError:
                return False
        return True

    def grade(self, subject: dict[str, Any], actor: str) -> RawGrade:
        claim_id = subject.get("claim_id")
        path = subject.get("path")
        if not isinstance(claim_id, str) or not isinstance(path, str):
            return RawGrade("error", {"code": "INVALID_SUBJECT_REF"})
        field = self._field(claim_id, path, actor)
        if field is None:
            return RawGrade("error", {"code": "CURRENT_FIELD_MISSING"})
        if (
            field["source_type"] != "extraction"
            or field["verification_state"] != "extracted"
            or (isinstance(subject.get("field_id"), str) and subject["field_id"] != field["id"])
        ):
            return RawGrade("error", {"code": "INVALID_CURRENT_EXTRACTED_FIELD"})
        source = field["source_ref"]
        if not isinstance(source, dict):
            return RawGrade("error", {"code": "INVALID_PROVENANCE"})
        document_id = source.get("document_id")
        page = source.get("page")
        bbox = normalized_bbox(source.get("bbox"))
        has_anchor = isinstance(source.get("anchor_text"), str) and bool(
            source["anchor_text"].strip()
        )
        has_vision = source.get("vision_bbox") is not None
        if (
            not isinstance(document_id, str)
            or not isinstance(page, int)
            or isinstance(page, bool)
            or page < 1
            or bbox is None
            or not (has_anchor or has_vision)
        ):
            return RawGrade("error", {"code": "INVALID_PROVENANCE"})
        with self.harness.engine.connect() as connection:
            belongs = connection.execute(
                text("SELECT 1 FROM documents WHERE id = :document_id AND claim_id = :claim_id"),
                {"document_id": document_id, "claim_id": claim_id},
            ).scalar()
        if belongs is None:
            return RawGrade("error", {"code": "INVALID_PROVENANCE"})
        page_key = f"pages/{document_id}/{page}.png"
        try:
            page_png = self.harness.blob_store.get(page_key)
            crop = crop_png(page_png, bbox)
        except Exception as error:  # noqa: BLE001 - corrupt/missing renders fail closed
            return RawGrade(
                "error",
                {"code": "CITATION_RENDER_ERROR", "error_type": type(error).__name__},
            )
        try:
            response = self.harness.model_call(
                "G-CITE",
                schema=GCITE_SCHEMA,
                inputs={
                    "_claim_id": claim_id,
                    "_document_id": document_id,
                    "crop_png": crop,
                    "value": field["value"],
                    "value_type": field["value_type"],
                },
            )["data"]
        except Exception as error:  # noqa: BLE001 - model failures are visible outcomes
            return RawGrade(
                "error",
                {"code": "MODEL_GRADER_ERROR", "error_type": type(error).__name__},
            )
        if response.get("value_present") is not True:
            return RawGrade("fail", {"code": "VALUE_NOT_PRESENT"})
        if field["value_type"] in {"money", "date"} and not self._exact_numeric(
            field["value_type"], response.get("observed_value"), field["value"]
        ):
            return RawGrade("fail", {"code": "EXACT_VALUE_MISMATCH"})
        return RawGrade("pass", {"code": "CITATION_VERIFIED"})


NUMERIC_CLAIM_PROPERTIES = {
    "text": {"type": "string"},
    "field_path": {"type": "string"},
    "source_kind": {"enum": ["claim_field", "calc_run", "savings_ledger"]},
    "source_ref": {"type": "string"},
    "observed_value": {"type": ["string", "integer"]},
    "value_type": {"type": "string"},
}
GNOTE_SCHEMA = {
    "type": "object",
    "required": [
        "numeric_claims",
        "unsupported_assertions",
        "missing_sections",
        "tone_ok",
    ],
    "additionalProperties": False,
    "properties": {
        "numeric_claims": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": NUMERIC_CLAIM_PROPERTIES,
                # PRD-08 identifies a source kind and ref. `field_path` is the
                # deprecated spelling of {claim_field, <path>} and is checked with
                # exactly the same comparison (register #236).
                "oneOf": [
                    {
                        "required": [
                            "text",
                            "source_kind",
                            "source_ref",
                            "observed_value",
                            "value_type",
                        ]
                    },
                    {"required": ["text", "field_path", "observed_value", "value_type"]},
                ],
            },
        },
        "unsupported_assertions": {"type": "array", "items": {"type": "string"}},
        "missing_sections": {"type": "array", "items": {"type": "string"}},
        "tone_ok": {"type": "boolean"},
    },
}
NUMERIC_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?")
COMMENTARY_NODE = re.compile(
    r'<div class="commentary" data-slot="(?P<slot>[a-z_]+)">(?P<body>.*?)</div>',
    re.DOTALL,
)
HTML_TAG = re.compile(r"<[^>]+>")


def money_display(cents: int) -> str:
    """Format integer KES cents exactly as the T-01 body renders them."""

    shillings, remainder = divmod(abs(cents), 100)
    sign = "-" if cents < 0 else ""
    if remainder == 0:
        return f"KES {sign}{shillings:,}"
    return f"KES {sign}{shillings:,}.{remainder:02d}"


class NoteGrader(Grader):
    """Grade T-01 commentary and independently verify every numeric observation.

    Only the three rendered commentary nodes are graded; cover text, citation
    labels, and the locked computed sections are excluded (register #232). An
    artifact with no commentary nodes is graded whole, which is strictly
    stricter than skipping it (register #236).
    """

    def __init__(self, harness: Any) -> None:
        super().__init__(harness, "G-NOTE", "artifact", "major")

    @staticmethod
    def _tokens(text_value: str) -> Counter[str]:
        return Counter(token.replace(",", "") for token in NUMERIC_TOKEN.findall(text_value))

    @staticmethod
    def _commentary(note: str) -> tuple[dict[str, str], str]:
        sections = {
            match.group("slot"): HTML_TAG.sub(" ", match.group("body"))
            for match in COMMENTARY_NODE.finditer(note)
        }
        if not sections:
            return {}, note
        return sections, "\n".join(sections[slot] for slot in sections)

    def _resolve(
        self, claim_id: str, numeric_claim: dict[str, Any], actor: str
    ) -> tuple[Any, str, str] | None:
        """Return (value, value_type, display) for one cited source, or None."""

        kind = numeric_claim.get("source_kind")
        reference = numeric_claim.get("source_ref")
        if kind is None:
            kind, reference = "claim_field", numeric_claim.get("field_path")
        if not isinstance(reference, str):
            return None
        if kind == "claim_field":
            try:
                _claim, fields, _blocked = self.harness.claim_service.hydrate_claim(
                    claim_id, actor, paths=[reference]
                )
            except Exception:  # noqa: BLE001 - inaccessible fields fail closed
                return None
            field = fields.get(reference)
            if field is None:
                return None
            display = (
                money_display(field.value)
                if field.value_type == "money"
                else str(field.value)
            )
            return field.value, field.value_type, display
        table, column = (
            ("calc_runs", "output") if kind == "calc_run" else ("savings_ledger", "saving")
        )
        with self.harness.engine.connect() as connection:
            value = connection.execute(
                text(
                    f"SELECT {column} FROM {table} WHERE id = :row_id "  # noqa: S608
                    "AND claim_id = :claim_id"
                ),
                {"row_id": reference, "claim_id": claim_id},
            ).scalar()
        value = _decoded_json(value)
        if not isinstance(value, int) or isinstance(value, bool):
            return None
        return value, "money", money_display(value)

    def grade(self, subject: dict[str, Any], actor: str) -> RawGrade:
        claim_id = subject.get("claim_id")
        blob_key = subject.get("blob_key")
        if (
            not isinstance(claim_id, str)
            or not isinstance(blob_key, str)
            or subject.get("template_id") != "T-01"
        ):
            return RawGrade("error", {"code": "INVALID_T01_SUBJECT"})
        try:
            note = self.harness.blob_store.get(blob_key).decode("utf-8")
        except Exception as error:  # noqa: BLE001 - missing/non-UTF8 artifacts are errors
            return RawGrade(
                "error",
                {"code": "ARTIFACT_READ_ERROR", "error_type": type(error).__name__},
            )
        config = self.harness.config["model_graders"]["G-NOTE"]
        sections, graded_text = self._commentary(note)
        try:
            response = self.harness.model_call(
                "G-NOTE",
                schema=GNOTE_SCHEMA,
                inputs={
                    "_claim_id": claim_id,
                    "note": graded_text,
                    "sections": sections,
                    "rubric_ref": config.get("rubric_ref"),
                    "required_section_ids": config.get("required_section_ids", []),
                },
            )["data"]
        except Exception as error:  # noqa: BLE001 - model failures are visible outcomes
            return RawGrade(
                "error",
                {"code": "MODEL_GRADER_ERROR", "error_type": type(error).__name__},
            )
        claims = response["numeric_claims"]
        reported_tokens: Counter[str] = Counter()
        for numeric_claim in claims:
            reported_tokens.update(self._tokens(numeric_claim["text"]))
        if reported_tokens != self._tokens(graded_text):
            return RawGrade("fail", {"code": "NUMERIC_TOKEN_OMISSION"})
        if (
            response["unsupported_assertions"]
            or response["missing_sections"]
            or response["tone_ok"] is not True
        ):
            return RawGrade("fail", {"code": "RUBRIC_FAILURE"})
        for numeric_claim in claims:
            resolved = self._resolve(claim_id, numeric_claim, actor)
            if resolved is None:
                return RawGrade("fail", {"code": "UNSUPPORTED_NUMBER"})
            value, value_type, display = resolved
            declared = numeric_claim["value_type"]
            if numeric_claim.get("source_kind") is None:
                if (
                    declared not in {"money", "date"}
                    or value_type != declared
                    or not CitationGrader._exact_numeric(
                        declared, numeric_claim["observed_value"], value
                    )
                ):
                    return RawGrade("fail", {"code": "STRUCTURED_VALUE_MISMATCH"})
                continue
            if (
                value_type != declared
                or numeric_claim["observed_value"] != value
                or self._tokens(numeric_claim["text"]) != self._tokens(display)
            ):
                return RawGrade("fail", {"code": "NUMERIC_SOURCE_MISMATCH"})
        return RawGrade("pass", {"code": "NOTE_VERIFIED"})


class CalcGrader(Grader):
    """Purely re-execute a recorded calculation from current claim inputs."""

    def __init__(self, harness: Any) -> None:
        super().__init__(harness, "G-CALC", "calc", "critical")

    @staticmethod
    def _normalise(field: Any) -> Any:
        if field.value_type == "date":
            return (date.fromisoformat(field.value) - date(1970, 1, 1)).days
        if field.value_type == "datetime":
            parsed = datetime.fromisoformat(field.value.replace("Z", "+00:00"))
            return (parsed.astimezone(UTC).date() - date(1970, 1, 1)).days
        return field.value

    def _latest_calc_run(self, claim_id: str, calc_id: str) -> str | None:
        with self.harness.engine.connect() as connection:
            return connection.execute(
                text(
                    "SELECT id FROM calc_runs WHERE claim_id = :claim_id "
                    "AND calc_id = :calc_id AND status = 'executed' "
                    "ORDER BY ts DESC, id DESC LIMIT 1"
                ),
                {"claim_id": claim_id, "calc_id": calc_id},
            ).scalar()

    def _current_inputs(self, row: dict[str, Any], definition: Any, actor: str) -> dict[str, Any]:
        paths = [
            path
            for path in (*definition.inputs.values(), *definition.optional_inputs.values())
            if not path.startswith(("pack.", "runtime."))
        ]
        _claim, fields, _blocked = self.harness.claim_service.hydrate_claim(
            row["claim_id"],
            actor,
            paths=paths,
        )
        inputs: dict[str, Any] = {}
        missing: list[str] = []
        for alias, path in definition.inputs.items():
            if path.startswith("runtime.latest_calc_run."):
                value = self._latest_calc_run(
                    row["claim_id"], path.removeprefix("runtime.latest_calc_run.")
                )
                if value is None:
                    missing.append(path)
                else:
                    inputs[alias] = value
            elif path.startswith("pack."):
                # No live motor calculation currently binds pack config. Refuse instead
                # of guessing if a future pack introduces one without a public accessor.
                missing.append(path)
            else:
                field = fields.get(path)
                if field is None:
                    missing.append(path)
                else:
                    inputs[alias] = self._normalise(field)
        for alias, path in definition.optional_inputs.items():
            field = fields.get(path)
            if field is not None:
                inputs[alias] = self._normalise(field)
        if missing:
            raise LookupError("current calculation inputs blocked")
        return inputs

    def grade(self, subject: dict[str, Any], actor: str) -> RawGrade:
        run_id = subject.get("calc_run_id")
        if not isinstance(run_id, str):
            return RawGrade("error", {"code": "INVALID_SUBJECT_REF"})
        with self.harness.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT id, calc_id, version, output, claim_id, pack_id, pack_version, "
                    "status FROM calc_runs WHERE id = :run_id"
                ),
                {"run_id": run_id},
            ).mappings().first()
        if row is None:
            return RawGrade("error", {"code": "CALC_RUN_NOT_FOUND"})
        data = dict(row)
        data["output"] = _decoded_json(data["output"])
        if data["status"] != "executed":
            return RawGrade("error", {"code": "CALC_REEXECUTION_BLOCKED"})
        try:
            definition = self.harness.runtime.calc_registry(
                data["pack_id"], data["pack_version"]
            ).get(data["calc_id"])
            inputs = self._current_inputs(data, definition, actor)
            output = definition.function(**inputs)
        except Exception as error:  # noqa: BLE001 - errors are visible grader outcomes
            return RawGrade(
                "error",
                {"code": "CALC_REEXECUTION_BLOCKED", "error_type": type(error).__name__},
            )
        matches = _canonical(output) == _canonical(data["output"])
        return RawGrade(
            "pass" if matches else "fail",
            {"code": "OUTPUT_MATCH" if matches else "OUTPUT_MISMATCH"},
        )


class RuleGrader(Grader):
    """Re-evaluate pinned JSONLogic against the immutable inputs snapshot."""

    def __init__(self, harness: Any) -> None:
        super().__init__(harness, "G-RULE", "rule", "critical")

    def grade(self, subject: dict[str, Any], actor: str) -> RawGrade:
        del actor
        run_id = subject.get("rule_run_id")
        if not isinstance(run_id, str):
            return RawGrade("error", {"code": "INVALID_SUBJECT_REF"})
        with self.harness.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT rule_id, fired, inputs_snapshot, pack_id, pack_version, status "
                    "FROM rule_runs WHERE id = :run_id"
                ),
                {"run_id": run_id},
            ).mappings().first()
        if row is None:
            return RawGrade("error", {"code": "RULE_RUN_NOT_FOUND"})
        data = dict(row)
        if data["status"] != "evaluated":
            return RawGrade("error", {"code": "RULE_REEVALUATION_BLOCKED"})
        try:
            definition = self.harness.runtime.rule_registry(
                data["pack_id"], data["pack_version"]
            ).get(data["rule_id"])
            fired = bool(
                jsonLogic(definition.when, _decoded_json(data["inputs_snapshot"]))
            )
        except Exception as error:  # noqa: BLE001 - errors are visible grader outcomes
            return RawGrade(
                "error",
                {"code": "RULE_REEVALUATION_ERROR", "error_type": type(error).__name__},
            )
        return RawGrade(
            "pass" if fired is bool(data["fired"]) else "fail",
            {"code": "FIRED_MATCH" if fired is bool(data["fired"]) else "FIRED_MISMATCH"},
        )


class SumGrader(Grader):
    """Check that breakdown lines reconstruct their linked C-02 reserve."""

    def __init__(self, harness: Any) -> None:
        super().__init__(harness, "G-SUM", "calc", "critical")

    def grade(self, subject: dict[str, Any], actor: str) -> RawGrade:
        del actor
        run_id = subject.get("calc_run_id")
        if not isinstance(run_id, str):
            return RawGrade("error", {"code": "INVALID_SUBJECT_REF"})
        with self.harness.engine.connect() as connection:
            row = connection.execute(
                text("SELECT claim_id, output FROM calc_runs WHERE id = :run_id"),
                {"run_id": run_id},
            ).mappings().first()
            output = None if row is None else _decoded_json(row["output"])
            if row is None or not isinstance(output, list) or not output:
                return RawGrade("error", {"code": "BREAKDOWN_SHAPE_REQUIRED"})
            lines = output
            if not all(
                isinstance(line, dict)
                and isinstance(line.get("amount"), int)
                and not isinstance(line.get("amount"), bool)
                and isinstance(line.get("parent_reserve_id"), str)
                for line in lines
            ):
                return RawGrade("fail", {"code": "INVALID_BREAKDOWN_LINE"})
            parents = {line["parent_reserve_id"] for line in lines}
            if len(parents) != 1:
                return RawGrade("fail", {"code": "PARENT_RESERVE_MISMATCH"})
            parent = connection.execute(
                text(
                    "SELECT output FROM calc_runs WHERE id = :parent AND claim_id = :claim_id "
                    "AND calc_id = 'C-02' AND status = 'executed'"
                ),
                {"parent": next(iter(parents)), "claim_id": row["claim_id"]},
            ).scalar()
        if not isinstance(parent, int) or isinstance(parent, bool):
            return RawGrade("error", {"code": "LINKED_C02_NOT_FOUND"})
        matches = _canonical(sum(line["amount"] for line in lines)) == _canonical(parent)
        return RawGrade(
            "pass" if matches else "fail",
            {"code": "SUM_MATCH" if matches else "SUM_MISMATCH"},
        )


class TemplateGrader(Grader):
    """Verify stored artifacts, required fields, verification, and signability."""

    def __init__(self, harness: Any) -> None:
        super().__init__(harness, "G-TPL", "artifact", "critical")

    def _merged_pack(self, subject: dict[str, Any]) -> RawGrade:
        """Deterministically verify a PRD-08 merged pack against its manifest."""

        blob_key = subject.get("blob_key")
        manifest = subject.get("manifest")
        digest = subject.get("sha256")
        if not isinstance(blob_key, str) or not isinstance(manifest, list) or not manifest:
            return RawGrade("error", {"code": "INVALID_MERGED_PACK_SUBJECT"})
        try:
            content = self.harness.blob_store.get(blob_key)
        except Exception as error:  # noqa: BLE001 - a missing artifact fails closed
            return RawGrade(
                "fail",
                {"code": "ARTIFACT_OR_INPUT_MISSING", "error_type": type(error).__name__},
            )
        if hashlib.sha256(content).hexdigest() != digest:
            return RawGrade("fail", {"code": "PACK_DIGEST_MISMATCH"})
        pages = 0
        for item in manifest:
            sources = item.get("sources")
            if not isinstance(sources, list) or not sources:
                return RawGrade(
                    "fail",
                    {"code": "PACK_ITEM_UNRESOLVED", "item": item.get("item_id")},
                )
            for source in sources:
                span = source.get("pack_pages")
                if (
                    not isinstance(span, list)
                    or len(span) != 2
                    or span[0] < 2
                    or span[1] < span[0]
                ):
                    return RawGrade("fail", {"code": "PACK_PAGE_RANGE_INVALID"})
                pages = max(pages, int(span[1]))
        try:
            reader = PdfReader(io.BytesIO(content))
        except Exception as error:  # noqa: BLE001 - unparseable output fails closed
            return RawGrade(
                "fail", {"code": "PACK_UNREADABLE", "error_type": type(error).__name__}
            )
        if len(reader.pages) < pages:
            return RawGrade("fail", {"code": "PACK_PAGE_COUNT_MISMATCH"})
        return RawGrade("pass", {"code": "MERGED_PACK_VALID", "items": len(manifest)})

    def grade(self, subject: dict[str, Any], actor: str) -> RawGrade:
        if subject.get("artifact_kind") == "merged_pack":
            return self._merged_pack(subject)
        claim_id = subject.get("claim_id")
        template_id = subject.get("template_id")
        blob_key = subject.get("blob_key")
        if not all(isinstance(value, str) for value in (claim_id, template_id, blob_key)):
            return RawGrade("error", {"code": "INVALID_SUBJECT_REF"})
        try:
            claim, _unused, _blocked = self.harness.claim_service.hydrate_claim(
                claim_id,
                actor,
                paths=[],
            )
            pack_id, separator, version = claim.pack_version.partition("@")
            if not separator:
                raise LookupError("malformed pack pin")
            definition = self.harness.runtime.template_registry(pack_id, version).get(template_id)
            _claim, fields, _blocked = self.harness.claim_service.hydrate_claim(
                claim_id,
                actor,
                paths=definition.required_fields,
            )
            content = self.harness.blob_store.get(blob_key).decode("utf-8")
        except Exception as error:  # noqa: BLE001 - errors are visible grader outcomes
            return RawGrade(
                "fail",
                {"code": "ARTIFACT_OR_INPUT_MISSING", "error_type": type(error).__name__},
            )
        if "{{" in content or "}}" in content:
            return RawGrade("fail", {"code": "STRICT_UNDEFINED_LEAK"})
        rank = {"extracted": 0, "system_confirmed": 1, "human_verified": 2}
        missing = [path for path in definition.required_fields if path not in fields]
        under = [
            path
            for path in definition.required_fields
            if path in fields
            and rank.get(fields[path].verification_state, -1) < rank[definition.min_verification]
        ]
        if missing or under:
            return RawGrade("fail", {"code": "REQUIRED_FIELDS_INVALID", "paths": missing + under})
        placeholder = "PENDING CAPTURE" in content
        if placeholder and subject.get("signable") is not False:
            return RawGrade("fail", {"code": "SIGNABLE_INCONSISTENT"})
        return RawGrade("pass", {"code": "TEMPLATE_VALID"})


class CommunicationGrader(Grader):
    """Apply deterministic AR-3 recipient, registry, and verification checks."""

    def __init__(self, harness: Any) -> None:
        super().__init__(harness, "G-COMM", "artifact", "critical")

    def grade(self, subject: dict[str, Any], actor: str) -> RawGrade:
        claim_id = subject.get("claim_id")
        template_id = subject.get("template_id")
        recipients = subject.get("to_party_ids")
        if (
            not isinstance(claim_id, str)
            or not isinstance(template_id, str)
            or not isinstance(recipients, list)
            or not recipients
            or not all(isinstance(value, str) for value in recipients)
        ):
            return RawGrade("error", {"code": "INVALID_SUBJECT_REF"})
        try:
            claim, _fields, _blocked = self.harness.claim_service.hydrate_claim(
                claim_id, actor, paths=[]
            )
            pack_id, separator, version = claim.pack_version.partition("@")
            if not separator:
                raise LookupError("malformed pack pin")
            definition = self.harness.runtime.template_registry(pack_id, version).get(
                template_id
            )
            with self.harness.engine.connect() as connection:
                party_ids = {
                    str(value)
                    for value in connection.execute(
                        text("SELECT id FROM parties WHERE claim_id = :claim_id"),
                        {"claim_id": claim_id},
                    ).scalars()
                }
            if set(recipients) - party_ids:
                return RawGrade("fail", {"code": "RECIPIENT_OUTSIDE_CLAIM"})
            if definition.status == "pending_capture":
                return RawGrade("pass", {"code": "REGISTERED_PENDING_TEMPLATE"})
            _claim, fields, _blocked = self.harness.claim_service.hydrate_claim(
                claim_id,
                actor,
                paths=definition.required_fields,
            )
        except Exception as error:  # noqa: BLE001 - visible deterministic failure
            return RawGrade(
                "fail",
                {"code": "TEMPLATE_OR_CLAIM_MISSING", "error_type": type(error).__name__},
            )
        rank = {"extracted": 0, "system_confirmed": 1, "human_verified": 2}
        invalid = [
            path
            for path in definition.required_fields
            if path not in fields
            or rank.get(fields[path].verification_state, -1)
            < rank[definition.min_verification]
        ]
        if invalid:
            return RawGrade("fail", {"code": "MERGE_FIELD_INVALID", "paths": invalid})
        return RawGrade("pass", {"code": "COMMUNICATION_VALID"})


class GraderRegistry:
    """Closed grader catalog with live and pending entries."""

    def __init__(self, harness: Any) -> None:
        entries = [
            CitationGrader(harness)
            if harness.model_client is not None
            else PendingGrader(
                harness,
                "G-CITE",
                "field",
                "critical",
                status="pending",
                blocked_on="model client not configured",
            ),
            ValueGrader(harness),
            CalcGrader(harness),
            RuleGrader(harness),
            SumGrader(harness),
            TemplateGrader(harness),
            NoteGrader(harness)
            if harness.model_client is not None
            else PendingGrader(
                harness,
                "G-NOTE",
                "artifact",
                "major",
                status="pending",
                blocked_on="model client not configured",
            ),
            PendingGrader(
                harness,
                "G-COMM",
                "artifact",
                "critical",
                status="pending",
                blocked_on="AR-3 runtime not installed",
            ),
            PendingGrader(
                harness,
                "G-PROC",
                "artifact",
                "major",
                status="pending",
                blocked_on="PRD-13 cop_steps.yaml and Phase-2 agent runs",
            ),
        ]
        self._entries = {entry.grader_id: entry for entry in entries}

    def register_external_validator(
        self, path: str, *, name: str, check: Callable[[Any], bool]
    ) -> None:
        """Forward a captured PRD-09 readback validator to the critical G-VAL."""

        grader = self._entries["G-VAL"]
        if not isinstance(grader, ValueGrader):
            raise RuntimeError("G-VAL is not the live value grader")
        grader.register_external_validator(path, name=name, check=check)

    def activate_gcomm(self) -> None:
        """Install deterministic G-COMM when its AR-3 producer is available."""

        self._entries["G-COMM"] = CommunicationGrader(self._entries["G-COMM"].harness)

    def ids(self) -> list[str]:
        return list(self._entries)

    def get(self, grader_id: str) -> Grader:
        try:
            return self._entries[grader_id]
        except KeyError as error:
            raise LookupError(f"unknown grader {grader_id!r}") from error


class EvalConsumer:
    """Map production output events to their deterministic live graders."""

    def __init__(self, harness: Any) -> None:
        self.harness = harness
        self._sessions = sessionmaker(bind=harness.engine, expire_on_commit=False)

    def _already_graded(self, grader_id: str, ref_key: str, ref_value: str) -> bool:
        with self._sessions() as session:
            runs = session.scalars(select(GraderRun).where(GraderRun.grader_id == grader_id))
            return any((run.subject_ref or {}).get(ref_key) == ref_value for run in runs)

    def _next_calc(self, claim_id: str, calc_id: str) -> dict[str, Any] | None:
        with self.harness.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, output FROM calc_runs WHERE claim_id = :claim_id "
                    "AND calc_id = :calc_id AND status = 'executed' ORDER BY ts, id"
                ),
                {"claim_id": claim_id, "calc_id": calc_id},
            ).mappings()
            for row in rows:
                if not self._already_graded("G-CALC", "calc_run_id", row["id"]):
                    return dict(row)
        return None

    def _next_rule(self, claim_id: str, rule_id: str) -> str | None:
        with self.harness.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id FROM rule_runs WHERE claim_id = :claim_id "
                    "AND rule_id = :rule_id AND status = 'evaluated' ORDER BY evaluated_at, id"
                ),
                {"claim_id": claim_id, "rule_id": rule_id},
            )
            for row in rows:
                if not self._already_graded("G-RULE", "rule_run_id", row[0]):
                    return str(row[0])
        return None

    def __call__(self, event: Any) -> None:
        payload = event.payload
        if event.type == "calc.executed" and payload.get("status") == "executed":
            run = self._next_calc(event.claim_id, payload.get("calc_id"))
            if run is None:
                return
            subject = {
                "calc_run_id": run["id"],
                "claim_id": event.claim_id,
                "source_event_id": event.id,
            }
            self.harness.grade("G-CALC", subject, actor="agent:eval")
            if isinstance(run["output"], list):
                self.harness.grade("G-SUM", subject, actor="agent:eval")
            return
        if event.type == "rule.evaluated" and payload.get("status") == "evaluated":
            run_id = self._next_rule(event.claim_id, payload.get("rule_id"))
            if run_id is not None:
                self.harness.grade(
                    "G-RULE",
                    {
                        "rule_run_id": run_id,
                        "claim_id": event.claim_id,
                        "source_event_id": event.id,
                    },
                    actor="agent:eval",
                )
            return
        if event.type == "template.rendered":
            subject = {
                "claim_id": event.claim_id,
                "template_id": payload.get("template_id"),
                "blob_key": payload.get("blob_key"),
                "signable": payload.get("signable"),
                "source_event_id": event.id,
            }
            if not self._already_graded("G-TPL", "source_event_id", event.id):
                self.harness.grade("G-TPL", subject, actor="agent:eval")
            if (
                payload.get("template_id") == "T-01"
                and self.harness.graders.get("G-NOTE").status == "live"
                and not self._already_graded("G-NOTE", "source_event_id", event.id)
            ):
                self.harness.grade("G-NOTE", subject, actor="agent:eval")
            return
        if event.type == "field.updated":
            field_id = payload.get("field_id")
            with self.harness.engine.connect() as connection:
                row = (
                    connection.execute(
                        text("SELECT source_type, path FROM claim_fields WHERE id = :field_id"),
                        {"field_id": field_id},
                    )
                    .mappings()
                    .first()
                )
            if row is None:
                return
            subject = {
                "claim_id": event.claim_id,
                "field_id": field_id,
                "path": row["path"],
                "source_event_id": event.id,
            }
            if row["source_type"] == "extraction":
                if not self._already_graded("G-VAL", "field_id", field_id):
                    self.harness.grade("G-VAL", subject, actor="agent:eval")
                if self.harness.graders.get("G-CITE").status == "live" and not self._already_graded(
                    "G-CITE", "field_id", field_id
                ):
                    self.harness.grade("G-CITE", subject, actor="agent:eval")
                return
            # PRD-09 §9.5: a projection readback is graded against its captured
            # validator before it can count as autonomy evidence.
            if row["source_type"] == "projection_readback" and not self._already_graded(
                "G-VAL", "field_id", field_id
            ):
                self.harness.grade("G-VAL", subject, actor="agent:eval")


__all__ = ["EvalConsumer", "GraderRegistry", "GraderResult", "RawGrade"]
