"""PRD-07 multi-assessor wait-all and deterministic R-07 selection."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from assessment_agent.cascade import AssessmentCascade, _money_from_document
from assessment_agent.trigger import ACTOR
from claim_core import FieldWrite, field_dictionary, new_ulid


class AssessmentSelection:
    """Select the lowest verified quote only after wait-all or human approval."""

    def __init__(self, app: Any, cascade: AssessmentCascade) -> None:
        self.app = app
        self.cascade = cascade

    def is_multi(self, claim_id: str) -> bool:
        _claim, fields, _blocked = self.app.state.claim_service.hydrate_claim(
            claim_id, ACTOR, paths=["assessment.multi_mode"]
        )
        value = fields.get("assessment.multi_mode")
        return value is not None and value.value is True

    def _dispatched(self, claim_id: str) -> list[dict[str, Any]]:
        return [
            dict(event["payload"])
            for event in self.cascade._events(claim_id, "assessment.dispatched")
            if isinstance(event["payload"].get("assessor_party_id"), str)
        ]

    def _reports(self, claim_id: str) -> list[dict[str, Any]]:
        return [
            dict(event["payload"])
            for event in self.cascade._events(
                claim_id, "assessment.report_received"
            )
            if isinstance(event["payload"].get("assessor_party_id"), str)
            and isinstance(event["payload"].get("document_id"), str)
        ]

    def _selected(self, claim_id: str) -> bool:
        return bool(
            self.cascade._events(claim_id, "assessment.selection_completed")
        )

    def _eligible_reports(self, claim_id: str) -> list[dict[str, Any]]:
        return [
            report
            for report in self._reports(claim_id)
            if self.cascade.report_ready(claim_id, report["document_id"])
        ]

    def _comparison_row(
        self, claim_id: str, report: dict[str, Any]
    ) -> dict[str, Any] | None:
        document_id = report["document_id"]
        agreed = self.cascade.document_field(
            claim_id, document_id, "assessment.agreed_quote"
        )
        if agreed is None or not isinstance(agreed.get("value"), int):
            return None
        pav = self.cascade.document_field(claim_id, document_id, "assessment.pav")
        extracted = self.cascade.extraction_fields(document_id)
        fee = _money_from_document(extracted.get("assessor_fee", {}).get("value"))
        flags = extracted.get("flags", {}).get("value", [])
        if not isinstance(flags, list):
            flags = []
        return {
            "assessor_party_id": report["assessor_party_id"],
            "vendor_id": report.get("vendor_id"),
            "agreed_quote": agreed["value"],
            "pav": pav.get("value") if pav is not None else None,
            "fee": fee,
            "flags": list(flags),
            "_document_id": document_id,
        }

    def _commit_selected(
        self, claim_id: str, document_id: str, selected_party_id: str
    ) -> None:
        dictionary = field_dictionary()
        writes: list[FieldWrite] = []
        for path in ("assessment.agreed_quote", "assessment.pav"):
            field = self.cascade.document_field(claim_id, document_id, path)
            if field is None:
                continue
            source_ref = (
                dict(field["source_ref"])
                if isinstance(field.get("source_ref"), dict)
                else {"document_id": document_id}
            )
            source_ref.update(
                {"rule_id": "R-07", "selected_party_id": selected_party_id}
            )
            writes.append(
                FieldWrite(
                    path=path,
                    value=field["value"],
                    value_type=dictionary[path].value_type,
                    source_type="system",
                    source_ref=source_ref,
                    verification_state="system_confirmed",
                )
            )
        if writes:
            self.app.state.claim_service.write_fields(claim_id, writes, ACTOR)

    def select(self, claim_id: str, reports: list[dict[str, Any]]) -> None:
        if self._selected(claim_id) or not reports:
            return
        rows = [self._comparison_row(claim_id, report) for report in reports]
        comparison = [row for row in rows if row is not None]
        if not comparison:
            return
        comparison.sort(key=lambda row: str(row["assessor_party_id"]))
        selected = min(
            comparison,
            key=lambda row: (row["agreed_quote"], str(row["assessor_party_id"])),
        )
        document_id = str(selected["_document_id"])
        selected_party_id = str(selected["assessor_party_id"])
        public_comparison = []
        for row in comparison:
            public = dict(row)
            public.pop("_document_id", None)
            public_comparison.append(public)
        self._commit_selected(claim_id, document_id, selected_party_id)
        self.cascade._emit(
            claim_id=claim_id,
            event_type="assessment.selection_completed",
            payload={
                "claim_id": claim_id,
                "selected_party_id": selected_party_id,
                "selected_document_id": document_id,
                "rule_id": "R-07",
                "comparison": public_comparison,
            },
            correlation_id=new_ulid(),
        )
        self.cascade.arm(claim_id, document_id)

    def maybe_select(self, claim_id: str) -> None:
        if self._selected(claim_id):
            return
        dispatched = self._dispatched(claim_id)
        reports = self._reports(claim_id)
        dispatched_ids = {
            row["assessor_party_id"] for row in dispatched
        }
        report_ids = {row["assessor_party_id"] for row in reports}
        if not dispatched_ids or report_ids != dispatched_ids:
            return
        eligible = self._eligible_reports(claim_id)
        if {row["assessor_party_id"] for row in eligible} != dispatched_ids:
            return
        self.select(claim_id, eligible)

    def report_received(self, claim_id: str, document_id: str) -> None:
        if self.is_multi(claim_id):
            self.maybe_select(claim_id)
            return
        self.cascade.arm(claim_id, document_id)

    def _open_partial(self, claim_id: str) -> bool:
        with self.app.state.engine.connect() as connection:
            row_id = connection.execute(
                text(
                    "SELECT id FROM review_items WHERE claim_id = :claim_id "
                    "AND type = 'PROCEED_PARTIAL' AND status = 'open' LIMIT 1"
                ),
                {"claim_id": claim_id},
            ).scalar()
        return isinstance(row_id, str)

    def _issue_partial(self, event: Any) -> None:
        claim_id = event.claim_id
        if (
            not isinstance(claim_id, str)
            or not self.is_multi(claim_id)
            or self._selected(claim_id)
            or self._open_partial(claim_id)
        ):
            return
        dispatched_ids = {
            row["assessor_party_id"] for row in self._dispatched(claim_id)
        }
        received_ids = {
            row["assessor_party_id"] for row in self._reports(claim_id)
        }
        received = sorted(dispatched_ids & received_ids)
        outstanding = sorted(dispatched_ids - received_ids)
        if not received or not outstanding:
            return
        comparison = []
        for report in self._eligible_reports(claim_id):
            row = self._comparison_row(claim_id, report)
            if row is not None:
                comparison.append(
                    {key: value for key, value in row.items() if key != "_document_id"}
                )
        comparison.sort(key=lambda row: str(row["assessor_party_id"]))
        self.cascade._emit(
            claim_id=claim_id,
            event_type="review.created",
            payload={
                "type": "PROCEED_PARTIAL",
                "subtype": "assessment_reports_outstanding",
                "received": received,
                "outstanding": outstanding,
                "comparison": comparison,
                "facts": {"received": received, "outstanding": outstanding},
                "risk": "assessment selection is waiting beyond the turnaround SLA",
                "recommendation": "approve only if selection may proceed with received reports",
                "resolution_schema": "PROCEED_PARTIAL@1",
            },
            correlation_id=event.id,
        )

    def _resolved_partial(self, event: Any) -> None:
        if (
            not isinstance(event.claim_id, str)
            or not isinstance(event.payload, dict)
            or event.payload.get("type") != "PROCEED_PARTIAL"
            or event.payload.get("resolution") != "approved"
        ):
            return
        reports = self._eligible_reports(event.claim_id)
        if reports:
            self.select(event.claim_id, reports)

    def consume(self, event: Any) -> None:
        if event.type == "sla.breached" and isinstance(event.payload, dict):
            if event.payload.get("definition_id") == "assessor_turnaround":
                self._issue_partial(event)
            return
        if event.type == "review.resolved":
            self._resolved_partial(event)


__all__ = ["AssessmentSelection"]
