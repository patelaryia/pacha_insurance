"""Structured T-01 assembly, governed commentary, grading, and hand-off.

Every rendered figure carries the exact integer KES cents of its current claim
field plus resolved provenance. Unresolved slots render a visible blocked
marker and never a synthetic zero or false (register #5/#229/#230).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from jinja2 import Environment, StrictUndefined, TemplateError
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from approval_pack_agent.config import (
    ApprovalPackConfig,
    money_display,
)
from approval_pack_agent.models import NoteDraft
from approval_pack_agent.resolver import ACTOR, Readiness, _json
from claim_core import ClaimCoreError, new_ulid
from doc_intel.llm import ModelBudgetExceeded, ModelWrapper

NUMERIC_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?")
WORD = re.compile(r"\S+")
COMMENTARY_SCHEMA = {
    "type": "object",
    "required": ["paragraphs"],
    "additionalProperties": False,
    "properties": {
        "paragraphs": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["template_slot", "content", "numbers_used"],
                "additionalProperties": False,
                "properties": {
                    "template_slot": {"type": "string"},
                    "content": {"type": "string"},
                    "numbers_used": {"type": "array", "items": {"type": "string"}},
                },
            },
        }
    },
}


def numeric_tokens(value: str) -> list[str]:
    """Return every numeric token in ``value`` with separators normalised out."""

    return [token.replace(",", "") for token in NUMERIC_TOKEN.findall(value)]


class NoteInputsInvalid(RuntimeError):
    """A committed field lacked resolved provenance; the note refuses to build."""


class CommentaryInvalid(RuntimeError):
    """Structured commentary failed validation after its one regeneration."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(errors))


@dataclass(frozen=True)
class NoteCandidate:
    """One rendered, not-yet-graded T-01 candidate."""

    body: dict[str, Any]
    blob_key: str
    render_event_id: str


class NoteBuilder:
    """Assemble the three ordered T-01 classes from durable, cited inputs."""

    def __init__(self, app: Any, config: ApprovalPackConfig) -> None:
        self.app = app
        self.config = config

    def _display(self, value: Any, value_type: str) -> str:
        if value_type == "money":
            return money_display(value)
        if value_type == "bool":
            return "Yes" if value else "No"
        return str(value)

    def computed(self, readiness: Readiness) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return the ordered computed slots and the blockers they raise."""

        note = self.config.note
        slots: list[dict[str, Any]] = []
        blockers: list[dict[str, Any]] = []
        marker = 0
        for slot_id in note["computed_slots"]:
            definition = note["slots"][slot_id]
            label = definition["label"]
            status = definition["status"]
            if status == "active":
                path = definition["field_path"]
                row = readiness.field_rows.get(path)
                if row is None:
                    raise NoteInputsInvalid(f"{path} is not committed")
                if not isinstance(row.source_ref, dict) or not row.source_ref:
                    raise NoteInputsInvalid(f"{path} has no resolved provenance")
                marker += 1
                slots.append(
                    {
                        "slot": slot_id,
                        "label": label,
                        "state": "resolved",
                        "locked": True,
                        "value": row.value,
                        "value_type": row.value_type,
                        "display": self._display(row.value, row.value_type),
                        "source_ref": {
                            "field_id": row.id,
                            "path": path,
                            "version": row.version,
                            "provenance": dict(row.source_ref),
                        },
                        "citation_marker": f"[{marker}]",
                    }
                )
                continue
            display = (
                definition["placeholder"]
                if status == "pending_capture"
                else note["blocked_marker"]
            )
            slots.append(
                {
                    "slot": slot_id,
                    "label": label,
                    "state": status,
                    "locked": True,
                    "display": display,
                    "blocker": definition["blocker"],
                    "source_ref": None,
                    "citation_marker": "",
                }
            )
            blockers.append(
                {"slot": slot_id, "state": status, "detail": definition["blocker"]}
            )
        return slots, blockers

    def _consistency(self, claim_id: str) -> dict[str, list[dict[str, Any]]]:
        with self.app.state.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, check_id, status, severity, evidence, created_at "
                    "FROM consistency_results WHERE claim_id = :claim_id "
                    "ORDER BY created_at, id"
                ),
                {"claim_id": claim_id},
            ).mappings()
            results: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                entry = {
                    "id": row["id"],
                    "check_id": row["check_id"],
                    "status": row["status"],
                    "severity": row["severity"],
                    "evidence": _json(row["evidence"]),
                }
                results.setdefault(row["check_id"], []).append(entry)
        return results

    def verification(
        self, claim_id: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Render only persisted structured CC evidence; never recompute a check."""

        note = self.config.note
        results = self._consistency(claim_id)
        slots: list[dict[str, Any]] = []
        blockers: list[dict[str, Any]] = []
        used: list[dict[str, Any]] = []
        for slot_id in note["verification_slots"]:
            definition = note["verification"][slot_id]
            label = definition["label"]
            if definition.get("status") == "blocked_on_inputs":
                slots.append(
                    {
                        "slot": slot_id,
                        "label": label,
                        "state": "blocked_on_inputs",
                        "locked": True,
                        "display": note["blocked_marker"],
                        "blocker": definition["blocker"],
                        "evidence": [],
                    }
                )
                blockers.append(
                    {
                        "slot": slot_id,
                        "state": "blocked_on_inputs",
                        "detail": definition["blocker"],
                    }
                )
                continue
            evidence = [
                entry
                for check_id in definition["check_ids"]
                for entry in results.get(check_id, [])
            ]
            if not evidence:
                slots.append(
                    {
                        "slot": slot_id,
                        "label": label,
                        "state": "blocked_on_inputs",
                        "locked": True,
                        "display": note["blocked_marker"],
                        "blocker": (
                            "no persisted "
                            f"{'/'.join(definition['check_ids'])} result for this claim"
                        ),
                        "evidence": [],
                    }
                )
                blockers.append(
                    {
                        "slot": slot_id,
                        "state": "blocked_on_inputs",
                        "detail": f"{'/'.join(definition['check_ids'])} evidence absent",
                    }
                )
                continue
            # A flag is copied verbatim; it can never be normalised to a pass.
            state = "flagged" if any(entry["status"] == "flagged" for entry in evidence) else (
                evidence[-1]["status"]
            )
            slots.append(
                {
                    "slot": slot_id,
                    "label": label,
                    "state": state,
                    "locked": True,
                    "display": state,
                    "evidence": [
                        {
                            "id": entry["id"],
                            "check_id": entry["check_id"],
                            "status": entry["status"],
                        }
                        for entry in evidence
                    ],
                }
            )
            used.extend(evidence)
        return slots, blockers, used

    def savings(self, claim_id: str) -> list[dict[str, Any]]:
        """Return immutable savings rows that carry citation evidence."""

        with self.app.state.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, kind, baseline_amount, achieved_amount, saving, evidence "
                    "FROM savings_ledger WHERE claim_id = :claim_id ORDER BY occurred_at, id"
                ),
                {"claim_id": claim_id},
            ).mappings()
            ledger = []
            for row in rows:
                evidence = _json(row["evidence"])
                citations = evidence.get("citations") if isinstance(evidence, dict) else None
                if not citations:
                    continue
                ledger.append(
                    {
                        "id": row["id"],
                        "kind": row["kind"],
                        "baseline_amount": row["baseline_amount"],
                        "achieved_amount": row["achieved_amount"],
                        "saving": row["saving"],
                        "citations": citations,
                    }
                )
        return ledger

    def verified_fields(self, readiness: Readiness) -> dict[str, dict[str, Any]]:
        """Return every active T-01 field at the verification floor, with provenance."""

        bundle: dict[str, dict[str, Any]] = {}
        for path, row in sorted(readiness.field_rows.items()):
            if not isinstance(row.source_ref, dict) or not row.source_ref:
                raise NoteInputsInvalid(f"{path} has no resolved provenance")
            bundle[path] = {
                "value": row.value,
                "value_type": row.value_type,
                "display": self._display(row.value, row.value_type),
                "field_id": row.id,
            }
        return bundle

    def allowed_numbers(
        self,
        verified: dict[str, dict[str, Any]],
        savings: list[dict[str, Any]],
    ) -> list[str]:
        """Derive the display-safe number tokens the model may reuse."""

        tokens: list[str] = []
        for entry in verified.values():
            tokens.extend(numeric_tokens(entry["display"]))
        for row in savings:
            for key in ("baseline_amount", "achieved_amount", "saving"):
                tokens.extend(numeric_tokens(money_display(row[key])))
        return sorted(set(tokens))


class CommentaryValidator:
    """Independently verify the structured commentary; never repair it."""

    def __init__(self, config: ApprovalPackConfig) -> None:
        self.config = config

    def validate(self, data: dict[str, Any], allowed: list[str]) -> list[str]:
        """Return every validation error; an empty list means the output is safe."""

        commentary = self.config.commentary
        sections = list(commentary["sections"])
        paragraphs = data.get("paragraphs")
        errors: list[str] = []
        if not isinstance(paragraphs, list) or [
            paragraph.get("template_slot") for paragraph in paragraphs
        ] != sections:
            return [f"paragraphs must be exactly {sections} in order"]
        allowed_multiset = sorted(allowed)
        for paragraph in paragraphs:
            slot = paragraph["template_slot"]
            content = paragraph["content"]
            declared = sorted(
                token.replace(",", "") for token in paragraph.get("numbers_used", [])
            )
            observed = sorted(numeric_tokens(content))
            if declared != observed:
                errors.append(
                    f"{slot}: numbers_used {declared} does not equal the numbers in the text "
                    f"{observed}"
                )
            for token in observed:
                if token not in allowed_multiset:
                    errors.append(f"{slot}: number {token} is not supported by claim inputs")
            lowered = content.casefold()
            for term in commentary["forbidden_terms"]:
                if term in lowered:
                    errors.append(f"{slot}: liability language {term!r} is not permitted")
            for american, british in commentary["american_spellings"].items():
                if re.search(rf"\b{re.escape(american)}\b", lowered):
                    errors.append(f"{slot}: use British English {british!r} not {american!r}")
            if slot == "incident_summary" and len(WORD.findall(content)) > int(
                commentary["incident_summary_max_words"]
            ):
                errors.append(
                    f"{slot}: exceeds {commentary['incident_summary_max_words']} words"
                )
        return errors


class CommentaryGenerator:
    """Call the commentary model through AR-4 with a redacted, governed audit."""

    def __init__(self, app: Any, config: ApprovalPackConfig, model_client: Any) -> None:
        self.app = app
        self.config = config
        self.model_client = model_client
        self.validator = CommentaryValidator(config)

    def _record(self, claim_id: str, *, cost_usd: float, model_id: str, status: str) -> None:
        self.app.state.claim_service.record_model_call(
            {
                "claim_id": claim_id,
                "task": self.config.commentary["task"],
                "purpose": self.config.commentary["task"],
                "prompt_ref": self.config.commentary["prompt_ref"],
                "tier": self.config.commentary["tier"],
                "model_id": model_id,
                "cost_usd": cost_usd,
                "status": status,
            }
        )

    @staticmethod
    def _as_datetime(value: Any) -> datetime | None:
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                return None
        return value if isinstance(value, datetime) else None

    def _spend(self, claim_id: str) -> tuple[Decimal, Decimal, Decimal]:
        """Sum recorded commentary spend by claim-day, claim-lifetime, platform-day."""

        task = self.config.commentary["task"]
        today = self.app.state.clock().date()
        claim_daily = claim_lifetime = platform_daily = Decimal(0)
        with self.app.state.engine.connect() as connection:
            rows = list(
                connection.execute(
                    text(
                        "SELECT claim_id, payload, occurred_at FROM events "
                        "WHERE type = 'model.called'"
                    )
                ).mappings()
            )
        for row in rows:
            payload = _json(row["payload"])
            detail = payload.get("detail") if isinstance(payload, dict) else None
            if not isinstance(detail, dict) or detail.get("task") != task:
                continue
            try:
                cost = Decimal(str(detail.get("cost_usd")))
            except (ArithmeticError, TypeError, ValueError):
                continue
            occurred_at = self._as_datetime(row["occurred_at"])
            same_day = occurred_at is not None and occurred_at.date() == today
            if row["claim_id"] == claim_id:
                claim_lifetime += cost
                if same_day:
                    claim_daily += cost
            if same_day:
                platform_daily += cost
        return claim_daily, claim_lifetime, platform_daily

    def _check_budget(self, claim_id: str, *, before_call: bool = False) -> None:
        """Refuse before or after a call once any configured ceiling is reached."""

        commentary = self.config.commentary
        used = dict(
            zip(
                ("claim_daily", "claim_lifetime", "platform_daily"),
                self._spend(claim_id),
                strict=True,
            )
        )
        limits = {
            "claim_daily": Decimal(str(commentary["claim_daily_budget_usd"])),
            "claim_lifetime": Decimal(str(commentary["claim_lifetime_budget_usd"])),
            "platform_daily": Decimal(str(commentary["platform_daily_budget_usd"])),
        }
        exceeded = sorted(
            key
            for key in limits
            if (used[key] >= limits[key] if before_call else used[key] > limits[key])
        )
        if exceeded:
            raise ModelBudgetExceeded(
                "pack note commentary budget exceeded: " + ", ".join(exceeded)
            )

    def generate(self, claim_id: str, bundle: dict[str, Any], allowed: list[str]) -> dict[str, Any]:
        """Generate once, regenerate at most once, then refuse.

        One wrapper spans both attempts so the configured per-call ceiling is a
        single budget, not one ceiling per regeneration (AR-4).
        """

        commentary = self.config.commentary
        wrapper = ModelWrapper(
            self.model_client,
            budget_ceiling_usd=float(commentary["max_cost_usd"]),
        )
        errors: list[str] = []
        for attempt in range(2):
            self._check_budget(claim_id, before_call=True)
            inputs = dict(bundle)
            if attempt == 1:
                inputs["validation_errors"] = list(errors)
            spent_before = wrapper.spent_usd
            result = wrapper.structured_call(
                tier=commentary["tier"], schema=COMMENTARY_SCHEMA, inputs=inputs
            )
            self._record(
                claim_id,
                cost_usd=wrapper.spent_usd - spent_before,
                model_id=result["model_id"],
                status="completed",
            )
            self._check_budget(claim_id)
            errors = self.validator.validate(result["data"], allowed)
            if not errors:
                return result["data"]
        raise CommentaryInvalid(errors)


class NoteService:
    """Own the T-01 candidate, its integrity gates, and the review hand-off."""

    def __init__(
        self,
        app: Any,
        config: ApprovalPackConfig,
        *,
        model_client: Any,
        store: Any,
    ) -> None:
        self.app = app
        self.config = config
        self.store = store
        self.builder = NoteBuilder(app, config)
        self.generator = CommentaryGenerator(app, config, model_client)
        self.sessions = sessionmaker(bind=app.state.engine, expire_on_commit=False)
        self._environment = Environment(
            undefined=StrictUndefined, autoescape=True, keep_trailing_newline=True
        )

    # -- helpers ---------------------------------------------------------------

    def _template(self, claim_id: str, actor: str) -> Any:
        claim, _fields, _blocked = self.app.state.claim_service.hydrate_claim(
            claim_id, actor, paths=[]
        )
        pack_id, separator, version = claim.pack_version.partition("@")
        if not separator:
            raise NoteInputsInvalid("claim pack pin is malformed")
        return self.app.state.cop_runtime.template_registry(pack_id, version).get("T-01")

    def _emit(
        self,
        *,
        claim_id: str,
        event_type: str,
        payload: dict[str, Any],
        correlation_id: str | None,
        actor: str = ACTOR,
    ) -> str:
        with self.sessions.begin() as session:
            event = self.app.state.record_event(
                session,
                claim_id=claim_id,
                event_type=event_type,
                payload=payload,
                actor=actor,
                correlation_id=correlation_id,
            )
            return event.id

    def bundle(
        self,
        readiness: Readiness,
        verified: dict[str, dict[str, Any]],
        consistency: list[dict[str, Any]],
        savings: list[dict[str, Any]],
        allowed: list[str],
    ) -> dict[str, Any]:
        """Build the allowlisted model input; no claim prose or contact enters it."""

        return {
            "task": self.config.commentary["task"],
            "prompt_ref": self.config.commentary["prompt_ref"],
            "locale": self.config.commentary["locale"],
            "sections": list(self.config.commentary["sections"]),
            "incident_summary_max_words": self.config.commentary["incident_summary_max_words"],
            "max_input_tokens": self.config.commentary["max_input_tokens"],
            "max_output_tokens": self.config.commentary["max_output_tokens"],
            "verified_fields": dict(verified),
            "consistency_results": [
                {"id": row["id"], "check_id": row["check_id"], "status": row["status"]}
                for row in consistency
            ],
            "savings_rows": [
                {
                    "id": row["id"],
                    "kind": row["kind"],
                    "baseline_amount": row["baseline_amount"],
                    "achieved_amount": row["achieved_amount"],
                    "saving": row["saving"],
                }
                for row in savings
            ],
            "allowed_numbers": list(allowed),
            "claim_status": readiness.status,
        }

    # -- candidate -------------------------------------------------------------

    def build_candidate(
        self,
        *,
        claim_id: str,
        actor: str,
        readiness: Readiness,
        merged_event_id: str,
        merged_payload: dict[str, Any],
        version: int,
    ) -> NoteCandidate:
        """Render one deterministic review artifact and publish its render event."""

        definition = self._template(claim_id, actor)
        computed, computed_blockers = self.builder.computed(readiness)
        verification, verification_blockers, consistency = self.builder.verification(claim_id)
        savings = self.builder.savings(claim_id)
        verified = self.builder.verified_fields(readiness)
        allowed = self.builder.allowed_numbers(verified, savings)
        bundle = self.bundle(readiness, verified, consistency, savings, allowed)
        data = self.generator.generate(claim_id, bundle, allowed)

        commentary_sections = [
            {
                "template_slot": paragraph["template_slot"],
                "label": self.config.note["commentary"][paragraph["template_slot"]]["label"],
                "content": paragraph["content"],
                "locked": False,
                "numbers_used": list(paragraph["numbers_used"]),
            }
            for paragraph in data["paragraphs"]
        ]
        blockers = computed_blockers + verification_blockers
        body = {
            "schema_version": 1,
            "template_id": "T-01",
            "template_version": definition.version,
            "merged_pack": {
                "version": merged_payload["version"],
                "event_id": merged_event_id,
                "sha256": merged_payload["sha256"],
            },
            "sections": [
                {"template_slot": "computed", "content": computed, "locked": True},
                {"template_slot": "verification", "content": verification, "locked": True},
                *[
                    {
                        "template_slot": section["template_slot"],
                        "content": section["content"],
                        "locked": False,
                        "numbers_used": section["numbers_used"],
                    }
                    for section in commentary_sections
                ],
            ],
            "blockers": blockers,
            "signable": False,
            "integrity": {
                "g_tpl_run_id": None,
                "g_note_run_id": None,
                "g_tpl_result": None,
                "g_note_result": None,
            },
        }
        artifact = self._render(
            definition,
            readiness=readiness,
            computed=computed,
            verification=verification,
            commentary=commentary_sections,
            blockers=blockers,
            merged_payload=merged_payload,
        )
        blob_key = f"approval-packs/{claim_id}/notes/v{version}/{new_ulid()}.html"
        self.store.put_immutable(blob_key, artifact, retention=self.config.retention)
        render_event_id = self._emit(
            claim_id=claim_id,
            event_type="template.rendered",
            payload={
                "template_id": "T-01",
                "template_version": definition.version,
                "channel": definition.channel,
                "note_draft_candidate_id": f"{claim_id}:{version}",
                "merged_pack_event_id": merged_event_id,
                "blob_key": blob_key,
                "signable": False,
            },
            correlation_id=merged_event_id,
        )
        return NoteCandidate(body=body, blob_key=blob_key, render_event_id=render_event_id)

    def _render(
        self,
        definition: Any,
        *,
        readiness: Readiness,
        computed: list[dict[str, Any]],
        verification: list[dict[str, Any]],
        commentary: list[dict[str, Any]],
        blockers: list[dict[str, Any]],
        merged_payload: dict[str, Any],
    ) -> bytes:
        context: dict[str, Any] = {
            path.replace(".", "_"): readiness.field_rows[path].value
            for path in definition.required_fields
        }
        context.update(
            {
                "computed_section": computed,
                "verification_section": verification,
                "commentary_section": [
                    {
                        "slot": section["template_slot"],
                        "label": section["label"],
                        "content": section["content"],
                    }
                    for section in commentary
                ],
                "blockers": [
                    {"slot": blocker["slot"], "detail": blocker["detail"]}
                    for blocker in blockers
                ],
                "signable_display": "no",
                "merged_pack_version": merged_payload["version"],
                "merged_pack_sha256": merged_payload["sha256"],
            }
        )
        if definition.body_path is None:
            raise NoteInputsInvalid("T-01 has no captured body")
        try:
            source = definition.body_path.read_text(encoding="utf-8")
            return self._environment.from_string(source).render(context).encode("utf-8")
        except (OSError, TemplateError, TypeError) as error:
            raise NoteInputsInvalid(f"T-01 refused to render: {error}") from error

    # -- integrity and persistence --------------------------------------------

    def grade(self, claim_id: str, candidate: NoteCandidate) -> dict[str, Any]:
        """Run both integrity gates synchronously through the public harness."""

        subject = {
            "claim_id": claim_id,
            "template_id": "T-01",
            "blob_key": candidate.blob_key,
            "signable": False,
            "source_event_id": candidate.render_event_id,
            "capability_id": "pack.note_draft",
        }
        tpl = self.app.state.eval_harness.grade("G-TPL", subject, actor="agent:eval")
        note = self.app.state.eval_harness.grade("G-NOTE", subject, actor="agent:eval")
        return {
            "g_tpl_run_id": tpl.grader_run_id,
            "g_note_run_id": note.grader_run_id,
            "g_tpl_result": tpl.result,
            "g_note_result": note.result,
        }

    def _next_version(self, claim_id: str) -> int:
        with self.app.state.engine.connect() as connection:
            highest = connection.execute(
                text("SELECT MAX(version) FROM note_drafts WHERE claim_id = :claim_id"),
                {"claim_id": claim_id},
            ).scalar()
        return int(highest or 0) + 1

    def persist(
        self, claim_id: str, *, version: int, body: dict[str, Any], status: str
    ) -> str:
        """Insert one version and supersede only an unsigned earlier draft."""

        draft_id = new_ulid()
        superseded: list[str] = []
        with self.sessions.begin() as session:
            if status == "in_review":
                superseded = [
                    str(row[0])
                    for row in session.execute(
                        text(
                            "SELECT id FROM note_drafts WHERE claim_id = :claim_id "
                            "AND status IN ('draft', 'in_review')"
                        ),
                        {"claim_id": claim_id},
                    )
                ]
                session.execute(
                    text(
                        "UPDATE note_drafts SET status = 'superseded' "
                        "WHERE claim_id = :claim_id AND status IN ('draft', 'in_review')"
                    ),
                    {"claim_id": claim_id},
                )
            session.add(
                NoteDraft(
                    id=draft_id,
                    claim_id=claim_id,
                    version=version,
                    body=body,
                    status=status,
                    edited_by=None,
                    signed_by=None,
                    signed_at=None,
                )
            )
        self._withdraw_reviews(claim_id, superseded)
        return draft_id

    def _withdraw_reviews(self, claim_id: str, draft_ids: list[str]) -> None:
        """Close the NOTE_REVIEW of every superseded draft: exactly one stays open."""

        if not draft_ids:
            return
        self.app.state.review_queue.backfill(ACTOR)
        with self.app.state.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, payload FROM review_items WHERE claim_id = :claim_id "
                    "AND type = 'NOTE_REVIEW' AND status = 'open' ORDER BY created_at, id"
                ),
                {"claim_id": claim_id},
            ).mappings()
            stale = [
                row["id"]
                for row in rows
                if (_json(row["payload"]) or {}).get("note_draft_id") in set(draft_ids)
            ]
        for review_id in stale:
            self.app.state.review_queue.cancel(
                review_id,
                actor=ACTOR,
                reason="a newer approval-note version superseded this draft",
            )

    def refuse(
        self,
        claim_id: str,
        *,
        subtype: str,
        facts: dict[str, Any],
        risk: str,
        recommendation: str,
        correlation_id: str | None,
    ) -> None:
        """Create one idempotent four-part EXCEPTION and change nothing else."""

        with self.app.state.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT payload FROM events WHERE claim_id = :claim_id "
                    "AND type = 'review.created' ORDER BY seq"
                ),
                {"claim_id": claim_id},
            ).scalars()
            for raw in rows:
                payload = _json(raw)
                if (
                    isinstance(payload, dict)
                    and payload.get("type") == "EXCEPTION"
                    and payload.get("subtype") == subtype
                    and payload.get("facts") == facts
                ):
                    return
        self._emit(
            claim_id=claim_id,
            event_type="review.created",
            payload={
                "type": "EXCEPTION",
                "subtype": subtype,
                "capability_id": "pack.note_draft",
                "facts": facts,
                "risk": risk,
                "recommendation": recommendation,
                "resolution_schema": "EXCEPTION@1",
            },
            correlation_id=correlation_id,
        )

    def hand_off(
        self,
        claim_id: str,
        *,
        draft_id: str,
        version: int,
        body: dict[str, Any],
        candidate: NoteCandidate,
        merged_payload: dict[str, Any],
        merged_event_id: str,
        actor: str,
    ) -> str:
        """Publish the drafted note and open exactly one NOTE_REVIEW."""

        self._emit(
            claim_id=claim_id,
            event_type="pack.note_drafted",
            payload={
                "note_draft_id": draft_id,
                "note_version": version,
                "merged_pack_event_id": merged_event_id,
                "merged_pack_version": merged_payload["version"],
                "merged_pack_sha256": merged_payload["sha256"],
                "review_artifact_blob_key": candidate.blob_key,
                "signable": False,
                "integrity": body["integrity"],
            },
            correlation_id=merged_event_id,
        )
        review_event_id = self._emit(
            claim_id=claim_id,
            event_type="review.created",
            payload={
                "type": "NOTE_REVIEW",
                "subtype": "approval_note",
                "capability_id": "pack.note_draft",
                "note_draft_id": draft_id,
                "note_version": version,
                "merged_pack_event_id": merged_event_id,
                "merged_pack_blob_key": merged_payload["blob_key"],
                "merged_pack_sha256": merged_payload["sha256"],
                "review_artifact_blob_key": candidate.blob_key,
                "blockers": body["blockers"],
                "signable": False,
                "grader_runs": body["integrity"],
                "facts": {"note_draft_id": draft_id, "note_version": version},
                "risk": "an unsigned approval note is waiting for officer review",
                "recommendation": "review the cited pack and note before signing",
                "resolution_schema": "NOTE_REVIEW@1",
            },
            correlation_id=merged_event_id,
        )
        claim, _fields, _blocked = self.app.state.claim_service.hydrate_claim(
            claim_id, actor, paths=[]
        )
        if claim.status == "RESERVED":
            self.app.state.claim_service.transition_claim(
                claim_id, "PACK_READY", {"note_draft_id": draft_id}, actor
            )
        return review_event_id


def guard_note_review(item: Any, action: str, payload: dict[str, Any], actor: str) -> None:
    """Refuse every PACKET-18 NOTE_REVIEW resolution without touching a row."""

    del payload, actor
    if item.subtype == "approval_note" and action in {"approve", "edit_approve", "reject"}:
        raise ClaimCoreError(
            409,
            "NOTE_REVIEW_UI_NOT_BUILT",
            "Approval-note review resolution ships with PACKET-19",
        )


__all__ = [
    "COMMENTARY_SCHEMA",
    "CommentaryGenerator",
    "CommentaryInvalid",
    "CommentaryValidator",
    "NoteBuilder",
    "NoteCandidate",
    "NoteInputsInvalid",
    "NoteService",
    "guard_note_review",
    "numeric_tokens",
]
