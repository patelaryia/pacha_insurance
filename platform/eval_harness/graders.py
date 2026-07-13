"""Deterministic PRD-03 graders and the closed nine-grader registry."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from json_logic import jsonLogic
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from doc_intel.validators import validate_field
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
        validator = self._validators.get(path)
        if validator is None:
            return RawGrade("error", {"code": "NO_SCHEMA_TARGET_MAPPING", "path": path})
        _claim, fields, _blocked = self.harness.claim_service.hydrate_claim(
            claim_id,
            actor,
            paths=[path],
        )
        field = fields.get(path)
        if field is None:
            return RawGrade("error", {"code": "CURRENT_FIELD_MISSING", "path": path})
        outcome = validate_field(validator, field.value, today=self.harness.clock().date())
        result: GradeOutcome = "pass" if outcome.outcome in {"pass", "not_applicable"} else "fail"
        return RawGrade(result, {"validator": validator, "outcome": outcome.outcome})


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

    def grade(self, subject: dict[str, Any], actor: str) -> RawGrade:
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


class GraderRegistry:
    """Closed grader catalog with live and pending entries."""

    def __init__(self, harness: Any) -> None:
        entries = [
            PendingGrader(
                harness,
                "G-CITE",
                "field",
                "critical",
                status="pending",
                blocked_on="PACKET-09 model crop verification",
            ),
            ValueGrader(harness),
            CalcGrader(harness),
            RuleGrader(harness),
            SumGrader(harness),
            TemplateGrader(harness),
            PendingGrader(
                harness,
                "G-NOTE",
                "artifact",
                "major",
                status="pending",
                blocked_on="PACKET-09 model rubric grader",
            ),
            PendingGrader(
                harness,
                "G-COMM",
                "artifact",
                "critical",
                status="pending",
                blocked_on="PRD-06 outbound communication producer",
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
            self.harness.grade(
                "G-TPL",
                {
                    "claim_id": event.claim_id,
                    "template_id": payload.get("template_id"),
                    "blob_key": payload.get("blob_key"),
                    "signable": payload.get("signable"),
                    "source_event_id": event.id,
                },
                actor="agent:eval",
            )
            return
        if event.type == "field.updated":
            field_id = payload.get("field_id")
            with self.harness.engine.connect() as connection:
                row = connection.execute(
                    text("SELECT source_type, path FROM claim_fields WHERE id = :field_id"),
                    {"field_id": field_id},
                ).mappings().first()
            if row is not None and row["source_type"] == "extraction":
                self.harness.grade(
                    "G-VAL",
                    {
                        "claim_id": event.claim_id,
                        "field_id": field_id,
                        "path": row["path"],
                        "source_event_id": event.id,
                    },
                    actor="agent:eval",
                )


__all__ = ["EvalConsumer", "GraderRegistry", "GraderResult", "RawGrade"]
