"""PRD-07 C1/C3/C4/C5 report cascade and append-only savings ledger."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from assessment_agent.models import SavingsLedger
from assessment_agent.trigger import ACTOR
from claim_core import Base, FieldWrite, field_dictionary, new_ulid

FINANCIAL_FLOOR = Decimal("0.90")
FEE_PATHS = frozenset(
    {"assessment.assessor_fee", "assessment.reinspection_fee"}
)


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _money_from_document(value: Any) -> int | None:
    """Normalise a shilling-denominated extracted value to integer KES cents."""

    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        return None
    if isinstance(value, str):
        candidate = value.strip().upper()
        for prefix in ("KES", "KSH.", "KSH"):
            if candidate.startswith(prefix):
                candidate = candidate[len(prefix) :].strip()
                break
        candidate = candidate.replace(",", "")
    else:
        candidate = value
    try:
        shillings = Decimal(candidate)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not shillings.is_finite():
        return None
    cents = shillings * 100
    if cents != cents.to_integral_value():
        return None
    return int(cents)


class AssessmentCascade:
    """Arm only verified reports, then execute the deterministic PRD-07 cascade."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.sessions = sessionmaker(bind=app.state.engine, expire_on_commit=False)
        self.selection: Any | None = None
        Base.metadata.create_all(app.state.engine, tables=[SavingsLedger.__table__])

    def bind_selection(self, selection: Any) -> None:
        self.selection = selection

    def _events(self, claim_id: str, event_type: str) -> list[dict[str, Any]]:
        with self.app.state.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, payload, actor FROM events WHERE claim_id = :claim_id "
                    "AND type = :event_type ORDER BY seq"
                ),
                {"claim_id": claim_id, "event_type": event_type},
            ).mappings()
            return [
                {**dict(row), "payload": _json(row["payload"])}
                for row in rows
            ]

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

    def _field_versions(self, claim_id: str, path: str) -> list[dict[str, Any]]:
        with self.app.state.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, value, source_type, source_ref, confidence, "
                    "verification_state, created_by, version, superseded_by "
                    "FROM claim_fields WHERE claim_id = :claim_id AND path = :path "
                    "ORDER BY version DESC"
                ),
                {"claim_id": claim_id, "path": path},
            ).mappings()
            return [
                {
                    **dict(row),
                    "value": _json(row["value"]),
                    "source_ref": _json(row["source_ref"]),
                }
                for row in rows
            ]

    def document_field(
        self, claim_id: str, document_id: str, path: str
    ) -> dict[str, Any] | None:
        for row in self._field_versions(claim_id, path):
            source_ref = row.get("source_ref")
            if isinstance(source_ref, dict) and source_ref.get("document_id") == document_id:
                return row
        return None

    def extraction_fields(self, document_id: str) -> dict[str, dict[str, Any]]:
        output = self.app.state.doc_intel.extraction_output(document_id)
        fields = output.get("fields") if isinstance(output, dict) else None
        if not isinstance(fields, list):
            return {}
        return {
            str(row["name"]): dict(row)
            for row in fields
            if isinstance(row, dict) and isinstance(row.get("name"), str)
        }

    def citation_fields(self, document_id: str) -> dict[str, dict[str, Any]]:
        output = self.app.state.doc_intel.citation_output(document_id)
        fields = output.get("fields") if isinstance(output, dict) else None
        if not isinstance(fields, list):
            return {}
        return {
            str(row["name"]): dict(row)
            for row in fields
            if isinstance(row, dict) and isinstance(row.get("name"), str)
        }

    @staticmethod
    def _meets_floor(field: dict[str, Any] | None) -> bool:
        if field is None:
            return False
        if field.get("verification_state") == "human_verified":
            return True
        confidence = field.get("confidence")
        try:
            return confidence is not None and Decimal(str(confidence)) >= FINANCIAL_FLOOR
        except (InvalidOperation, ValueError):
            return False

    def report_ready(self, claim_id: str, document_id: str) -> bool:
        extracted = self.extraction_fields(document_id)
        def qualified(path: str) -> bool:
            return any(
                isinstance(row.get("source_ref"), dict)
                and row["source_ref"].get("document_id") == document_id
                and self._meets_floor(row)
                for row in self._field_versions(claim_id, path)
            )

        if not qualified("assessment.agreed_quote"):
            return False
        if "pav" not in extracted:
            return True
        return qualified("assessment.pav")

    def _current_claim(self, claim_id: str) -> Any:
        claim, _fields, _blocked = self.app.state.claim_service.hydrate_claim(
            claim_id, ACTOR, paths=[]
        )
        return claim

    def _transition_report_received(self, claim_id: str, document_id: str) -> None:
        claim = self._current_claim(claim_id)
        if claim.status == "IN_ASSESSMENT":
            self.app.state.claim_service.transition_claim(
                claim_id,
                "REPORT_RECEIVED",
                {"trigger": "assessor_report_parsed", "document_id": document_id},
                ACTOR,
            )

    def _write_off(self, claim_id: str, document_id: str) -> bool:
        result = self.app.state.cop_runtime.evaluate("R-05", claim_id, ACTOR)
        if result.status != "evaluated" or result.fired is not True:
            return False
        self.app.state.cop_runtime.execute_outcome(result, ACTOR)
        claim = self._current_claim(claim_id)
        if claim.status == "REPORT_RECEIVED":
            self.app.state.claim_service.transition_claim(
                claim_id,
                "WRITE_OFF",
                {"trigger": "R-05", "document_id": document_id},
                ACTOR,
            )
        return True

    def _latest_calc_run(
        self, claim_id: str, calc_id: str, *, status: str
    ) -> dict[str, Any] | None:
        with self.app.state.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT id, inputs, output FROM calc_runs WHERE claim_id = :claim_id "
                    "AND calc_id = :calc_id AND status = :status "
                    "ORDER BY ts DESC, id DESC LIMIT 1"
                ),
                {"claim_id": claim_id, "calc_id": calc_id, "status": status},
            ).mappings().first()
        if row is None:
            return None
        return {
            **dict(row),
            "inputs": _json(row["inputs"]),
            "output": _json(row["output"]),
        }

    def _projection_exists(self, claim_id: str, inputs: Any) -> bool:
        run_ids = {
            event["payload"].get("calc_run_id")
            for event in self._events(claim_id, "projection.requested")
            if isinstance(event.get("payload"), dict)
            and isinstance(event["payload"].get("calc_run_id"), str)
        }
        if not run_ids:
            return False
        with self.app.state.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, inputs FROM calc_runs WHERE claim_id = :claim_id "
                    "AND calc_id = 'C-02' AND status = 'executed'"
                ),
                {"claim_id": claim_id},
            ).mappings()
            return any(
                row["id"] in run_ids and _json(row["inputs"]) == inputs
                for row in rows
            )

    def _reserves(self, claim_id: str) -> None:
        if self._current_claim(claim_id).status == "WRITE_OFF":
            return
        result = self.app.state.cop_runtime.execute_calc("C-02", claim_id, ACTOR)
        if result.status != "executed":
            return
        run = self._latest_calc_run(claim_id, "C-02", status="executed")
        if run is None or not isinstance(result.output, int) or isinstance(result.output, bool):
            return
        if self._projection_exists(claim_id, run["inputs"]):
            return
        _claim, fields, _blocked = self.app.state.claim_service.hydrate_claim(
            claim_id, ACTOR, paths=["reserve.total"]
        )
        current = fields.get("reserve.total")
        if current is not None and current.verification_state == "human_verified":
            return
        definition = field_dictionary()["reserve.total"]
        self.app.state.claim_service.write_fields(
            claim_id,
            [
                FieldWrite(
                    path="reserve.total",
                    value=result.output,
                    value_type=definition.value_type,
                    source_type="calc",
                    source_ref={"calc_id": "C-02", "calc_run_id": run["id"]},
                    verification_state="system_confirmed",
                )
            ],
            ACTOR,
        )
        self._emit(
            claim_id=claim_id,
            event_type="projection.requested",
            payload={
                "claim_id": claim_id,
                "calc_run_id": run["id"],
                "reserve_total": result.output,
            },
            correlation_id=run["id"],
        )
        self.app.state.cop_runtime.execute_calc("C-03", claim_id, ACTOR)

    def _header_exists(self, claim_id: str) -> bool:
        with self.app.state.engine.connect() as connection:
            row_id = connection.execute(
                text(
                    "SELECT id FROM savings_ledger WHERE claim_id = :claim_id "
                    "AND kind = 'assessment_negotiation' LIMIT 1"
                ),
                {"claim_id": claim_id},
            ).scalar()
        return isinstance(row_id, str)

    @staticmethod
    def _citation(field: dict[str, Any] | None) -> dict[str, Any] | None:
        if field is None or not isinstance(field.get("source_ref"), dict):
            return None
        source_ref = field["source_ref"]
        mode = source_ref.get("citation_mode")
        if (
            not isinstance(source_ref.get("document_id"), str)
            or mode not in {"anchor_text", "vision_bbox"}
            or not isinstance(source_ref.get("bbox"), list)
        ):
            return None
        if mode == "anchor_text" and not isinstance(source_ref.get("anchor_text"), str):
            return None
        if mode == "vision_bbox" and source_ref.get("vision_verified") is not True:
            return None
        return dict(source_ref)

    @staticmethod
    def _resolved_line_citation(
        document_id: str, field: dict[str, Any]
    ) -> dict[str, Any] | None:
        citation = field.get("citation")
        mode = field.get("citation_mode")
        if (
            field.get("citation_failed") is True
            or mode not in {"anchor_text", "vision_bbox"}
            or not isinstance(field.get("page"), int)
            or not isinstance(citation, dict)
            or not isinstance(citation.get("bbox"), list)
        ):
            return None
        resolved = {
            "document_id": document_id,
            "page": field["page"],
            "bbox": list(citation["bbox"]),
            "citation_mode": mode,
        }
        if mode == "anchor_text":
            anchor = field.get("anchor_text")
            if not isinstance(anchor, str) or not anchor:
                return None
            resolved["anchor_text"] = anchor
        elif citation.get("vision_verified") is not True:
            return None
        else:
            resolved["vision_verified"] = True
        return resolved

    def _savings(self, claim_id: str, document_id: str) -> None:
        if self._header_exists(claim_id):
            return
        result = self.app.state.cop_runtime.execute_calc("C-05", claim_id, ACTOR)
        if result.status != "executed":
            return
        run = self._latest_calc_run(claim_id, "C-05", status="executed")
        if run is None:
            return
        estimate = self._field_versions(claim_id, "assessment.estimate_total")
        agreed = self.document_field(
            claim_id, document_id, "assessment.agreed_quote"
        )
        if not estimate or agreed is None:
            return
        baseline = estimate[0].get("value")
        achieved = agreed.get("value")
        if (
            not isinstance(baseline, int)
            or isinstance(baseline, bool)
            or not isinstance(achieved, int)
            or isinstance(achieved, bool)
        ):
            return
        estimate_citation = self._citation(estimate[0])
        agreed_citation = self._citation(agreed)
        if estimate_citation is None or agreed_citation is None:
            return
        citations = [estimate_citation, agreed_citation]
        cited = self.citation_fields(document_id)
        line_field = cited.get("supplier_lines", {})
        raw_lines = line_field.get("value", [])
        if not isinstance(raw_lines, list):
            raw_lines = []
        complete: list[tuple[int, int, dict[str, Any]]] = []
        incomplete: list[dict[str, Any]] = []
        line_citation = self._resolved_line_citation(document_id, line_field)
        for index, raw in enumerate(raw_lines):
            if not isinstance(raw, dict):
                incomplete.append({"index": index, "reason": "invalid_line"})
                continue
            garage = _money_from_document(raw.get("garage_price"))
            supplier = _money_from_document(raw.get("supplier_price"))
            if garage is None or supplier is None:
                incomplete.append(
                    {
                        "index": index,
                        "part": raw.get("part"),
                        "supplier": raw.get("supplier"),
                        "reason": "missing_or_invalid_price",
                    }
                )
                continue
            if line_citation is None:
                incomplete.append(
                    {
                        "index": index,
                        "part": raw.get("part"),
                        "supplier": raw.get("supplier"),
                        "reason": "unresolved_citation",
                    }
                )
                continue
            complete.append((garage, supplier, dict(raw)))
        header_evidence: dict[str, Any] = {
            "calc_run_id": run["id"],
            "citations": citations,
        }
        if incomplete:
            header_evidence["incomplete_lines"] = incomplete
        now = self.app.state.clock()
        with self.sessions.begin() as session:
            header = SavingsLedger(
                id=new_ulid(),
                claim_id=claim_id,
                kind="assessment_negotiation",
                baseline_amount=baseline,
                achieved_amount=achieved,
                evidence=header_evidence,
                vendor_id=None,
                occurred_at=now,
            )
            session.add(header)
            session.flush()
            self.app.state.record_event(
                session,
                claim_id=claim_id,
                event_type="savings.recorded",
                payload={
                    "claim_id": claim_id,
                    "savings_id": header.id,
                    "kind": header.kind,
                    "calc_run_id": run["id"],
                },
                actor=ACTOR,
                correlation_id=run["id"],
            )
            for garage, supplier, raw in complete:
                line = SavingsLedger(
                    id=new_ulid(),
                    claim_id=claim_id,
                    kind="supplier_substitution",
                    baseline_amount=garage,
                    achieved_amount=supplier,
                    evidence={
                        "document_id": document_id,
                        "supplier_name": raw.get("supplier"),
                        "part": raw.get("part"),
                        "citation": line_citation,
                    },
                    vendor_id=None,
                    occurred_at=now,
                )
                session.add(line)
                session.flush()
                self.app.state.record_event(
                    session,
                    claim_id=claim_id,
                    event_type="savings.recorded",
                    payload={
                        "claim_id": claim_id,
                        "savings_id": line.id,
                        "kind": line.kind,
                        "document_id": document_id,
                    },
                    actor=ACTOR,
                    correlation_id=run["id"],
                )

    def _review_once(
        self,
        claim_id: str,
        *,
        item_type: str,
        subtype: str,
        identity: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        for event in self._events(claim_id, "review.created"):
            existing = event["payload"]
            if (
                existing.get("type") == item_type
                and existing.get("subtype") == subtype
                and all(existing.get(key) == value for key, value in identity.items())
            ):
                return
        self._emit(
            claim_id=claim_id,
            event_type="review.created",
            payload={"type": item_type, "subtype": subtype, **identity, **payload},
            correlation_id=new_ulid(),
        )

    def _flags(self, claim_id: str, document_id: str) -> None:
        flags = self.extraction_fields(document_id).get("flags", {}).get("value", [])
        if not isinstance(flags, list):
            return
        for flag in flags:
            if not isinstance(flag, str) or not flag.strip():
                continue
            value = flag.strip()
            self._review_once(
                claim_id,
                item_type="CONSISTENCY_FLAG",
                subtype="assessor_report_flag",
                identity={"document_id": document_id, "flag": value},
                payload={
                    "capability_id": "assessment.consistency_flag",
                    "facts": {"flag": value, "document_id": document_id},
                    "risk": "assessor flag requires an officer decision",
                    "recommendation": "review the cited assessor report flag",
                    "resolution_schema": "CONSISTENCY_FLAG@1",
                },
            )

    def arm(self, claim_id: str, document_id: str) -> None:
        if not self.report_ready(claim_id, document_id):
            return
        self._transition_report_received(claim_id, document_id)
        if self._write_off(claim_id, document_id):
            return
        self._savings(claim_id, document_id)
        self._flags(claim_id, document_id)
        self._reserves(claim_id)

    def _selected_event(self, claim_id: str) -> dict[str, Any] | None:
        events = self._events(claim_id, "assessment.selection_completed")
        return events[-1] if events else None

    def selected_document(self, claim_id: str) -> str | None:
        selected = self._selected_event(claim_id)
        if selected is not None:
            document_id = selected["payload"].get("selected_document_id")
            return document_id if isinstance(document_id, str) else None
        reports = self._events(claim_id, "assessment.report_received")
        if len(reports) == 1:
            document_id = reports[0]["payload"].get("document_id")
            return document_id if isinstance(document_id, str) else None
        return None

    def _watch_override(self, claim_id: str, field: Any) -> None:
        selected = self._selected_event(claim_id)
        if selected is None or field.source_type != "human":
            return
        payload = selected["payload"]
        chosen = next(
            (
                row
                for row in payload.get("comparison", [])
                if row.get("assessor_party_id") == payload.get("selected_party_id")
            ),
            None,
        )
        if not isinstance(chosen, dict) or chosen.get("agreed_quote") == field.value:
            return
        self._review_once(
            claim_id,
            item_type="EXCEPTION",
            subtype="selection_overridden",
            identity={"selected_event_id": selected["id"]},
            payload={
                "facts": {
                    "selected_agreed_quote": chosen.get("agreed_quote"),
                    "human_agreed_quote": field.value,
                },
                "risk": "the officer value differs from deterministic R-07 selection",
                "recommendation": "confirm and document the assessor-selection override",
                "resolution_schema": "EXCEPTION@1",
            },
        )

    def consume(self, event: Any) -> None:
        if event.type != "field.updated" or not isinstance(event.claim_id, str):
            return
        path = event.payload.get("path") if isinstance(event.payload, dict) else None
        if path not in FEE_PATHS | {"assessment.agreed_quote", "assessment.pav"}:
            return
        _claim, fields, _blocked = self.app.state.claim_service.hydrate_claim(
            event.claim_id, ACTOR, paths=[path]
        )
        field = fields.get(path)
        if field is None:
            return
        if path == "assessment.agreed_quote":
            self._watch_override(event.claim_id, field)
            if field.source_type == "human" and self._selected_event(event.claim_id) is not None:
                return
        if path in FEE_PATHS:
            if self.selected_document(event.claim_id) is not None:
                self._reserves(event.claim_id)
            return
        source_ref = field.source_ref if isinstance(field.source_ref, dict) else {}
        document_id = source_ref.get("document_id")
        if not isinstance(document_id, str):
            return
        if not any(
            row["payload"].get("document_id") == document_id
            for row in self._events(event.claim_id, "assessment.report_received")
        ):
            return
        if self.selection is not None and self.selection.is_multi(event.claim_id):
            self.selection.maybe_select(event.claim_id)
        else:
            self.arm(event.claim_id, document_id)


__all__ = [
    "AssessmentCascade",
    "FINANCIAL_FLOOR",
    "_json",
    "_money_from_document",
]
