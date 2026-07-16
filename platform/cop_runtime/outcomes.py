"""Execution of the six closed PRD-02 outcome verbs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from claim_core import FieldWrite, field_dictionary
from cop_runtime.contracts import REVIEW_ITEM_TYPES
from cop_runtime.rules import RuleResult
from cop_runtime.templates import TemplateRenderBlocked


@dataclass(frozen=True)
class OutcomeResult:
    """Public summary of one fired outcome execution."""

    action: str
    detail: dict[str, Any]


class OutcomeExecutor:
    """Dispatch fired rule results without introducing any external side effect."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self._claim_service = runtime._claim_service
        self._sessions = runtime._sessions

    @staticmethod
    def _provenance(result: RuleResult) -> dict[str, str]:
        return {
            "rule_id": result.rule_id,
            "rule_version": result.rule_version,
            "rule_run_id": result.rule_run_id,
        }

    def _record_review(
        self,
        result: RuleResult,
        *,
        actor: str,
        payload: dict[str, Any],
    ) -> None:
        item_type = payload.get("type")
        if item_type not in REVIEW_ITEM_TYPES:
            raise ValueError(f"Review type {item_type!r} is outside the closed enum")
        with self._sessions.begin() as session:
            self._claim_service.record_event(
                session,
                claim_id=result.claim_id,
                event_type="review.created",
                payload=payload,
                actor=actor,
                correlation_id=None,
            )

    def _set_field(
        self,
        result: RuleResult,
        outcome: dict[str, Any],
        *,
        actor: str,
    ) -> OutcomeResult:
        path = outcome.get("path")
        if not isinstance(path, str) or path not in field_dictionary():
            raise ValueError(f"Outcome field path {path!r} is not registered")
        if "value" in outcome:
            value = outcome["value"]
        else:
            value_from = outcome.get("value_from")
            value_map = outcome.get("value_map")
            if not isinstance(value_from, str) or not isinstance(value_map, dict):
                raise ValueError("set_field requires value or value_from with value_map")
            if value_from not in result.inputs_snapshot:
                raise ValueError(f"Outcome input alias {value_from!r} is unavailable")
            key = result.inputs_snapshot[value_from]
            try:
                value = value_map[key]
            except (KeyError, TypeError) as error:
                raise ValueError(f"Outcome value map has no entry for {key!r}") from error

        definition = field_dictionary()[path]
        provenance = self._provenance(result)
        self._claim_service.write_fields(
            result.claim_id,
            [
                FieldWrite(
                    path=path,
                    value=value,
                    value_type=definition.value_type,
                    source_type="rule",
                    source_ref=provenance,
                    verification_state="system_confirmed",
                )
            ],
            actor,
        )
        return OutcomeResult("set_field", {"path": path, **provenance})

    def execute(self, result: RuleResult, *, actor: str) -> OutcomeResult:
        """Execute one fired rule outcome through its packet-defined consumer."""

        if result.fired is not True or result.outcome is None:
            raise ValueError("Only fired rule results can execute outcomes")
        outcome = result.outcome
        action = outcome.get("action")
        if action == "set_field":
            return self._set_field(result, outcome, actor=actor)
        if action == "route_review":
            route = outcome.get("route")
            pack = self._runtime._pack(result.pack_id, result.pack_version)
            review_routes = pack.config["review_routes"]
            if not isinstance(route, str) or route not in review_routes:
                raise LookupError(f"No review-item mapping exists for route {route!r}")
            payload = {
                "type": review_routes[route],
                "route": route,
                "role": outcome.get("role"),
                "rule_id": result.rule_id,
                "rule_run_id": result.rule_run_id,
            }
            self._record_review(result, actor=actor, payload=payload)
            return OutcomeResult("route_review", payload)
        if action == "propose_decline":
            exception = outcome.get("exception")
            context_paths = (
                exception.get("context_fields", [])
                if isinstance(exception, dict)
                else []
            )
            payload = {
                "type": "DRAFT_RELEASE",
                "subtype": "decline_draft",
                "draft_template": outcome.get("draft_template"),
                "capability_id": "triage.decline_draft",
                "context_fields": [
                    {"path": path, "status": "pending_field_registration"}
                    for path in context_paths
                    if isinstance(path, str)
                ],
                "rule_id": result.rule_id,
                "rule_run_id": result.rule_run_id,
            }
            self._record_review(result, actor=actor, payload=payload)
            return OutcomeResult("propose_decline", payload)
        if action == "block":
            return OutcomeResult("block", self._provenance(result))
        if action == "emit_event":
            template_id = outcome.get("draft_template")
            if not isinstance(template_id, str):
                raise ValueError("emit_event requires draft_template")
            try:
                rendered = self._runtime.render(
                    template_id,
                    result.claim_id,
                    actor=actor,
                )
            except TemplateRenderBlocked:
                payload = {
                    "type": "EXCEPTION",
                    "subtype": "template_pending_capture",
                    "template_id": template_id,
                    "rule_id": result.rule_id,
                    "rule_run_id": result.rule_run_id,
                }
                self._record_review(result, actor=actor, payload=payload)
                return OutcomeResult("emit_event", payload)
            return OutcomeResult(
                "emit_event",
                {"template_id": template_id, "blob_key": rendered.blob_key},
            )
        if action == "route_approval":
            raise NotImplementedError("route_approval execution belongs to PRD-08")
        raise ValueError(f"Unsupported outcome action {action!r}")


__all__ = ["OutcomeExecutor", "OutcomeResult"]
