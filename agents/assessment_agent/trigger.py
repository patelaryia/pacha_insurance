"""Estimate-verified trigger and dual-path mode decision for PRD-07 §7.3."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from agent_runtime import Action
from claim_core import STATE_METADATA, ClaimState, new_ulid
from doc_intel.llm import ModelBudgetExceeded, ModelWrapper

ACTOR = "agent:assessment"
COMMITTED_STATES = frozenset({"extracted", "human_verified", "system_confirmed"})
PROGRESSED_STATES = frozenset(
    {
        "IN_ASSESSMENT",
        "REPORT_RECEIVED",
        "REGISTERED",
        "RESERVED",
        "PACK_READY",
        "IN_APPROVAL",
        "APPROVED",
        "IN_REPAIR",
        "REINSPECTION",
        "RELEASED",
        "WRITE_OFF",
        "CLIENT_ELECTION",
        "SURRENDER_CHECKLIST",
        "SALVAGE_BIDDING",
        "RETAINED",
        "SETTLEMENT",
        "SETTLED",
        "CLOSED",
    }
)
SHADOW_SCHEMA = {
    "type": "object",
    "required": ["mode", "rationale", "confidence"],
    "properties": {
        "mode": {"enum": ["desk", "physical"]},
        "rationale": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "additionalProperties": False,
}


class AssessmentTrigger:
    """Advance eligible claims and issue one governed mode card per estimate window."""

    def __init__(self, app: Any, model_client: Any, shadow_config: dict[str, Any]) -> None:
        self.app = app
        self.model_client = model_client
        self.shadow_config = dict(shadow_config)
        self.sessions = sessionmaker(bind=app.state.engine, expire_on_commit=False)

    def _emit(
        self,
        *,
        claim_id: str,
        event_type: str,
        payload: dict[str, Any],
        correlation_id: str,
    ) -> str:
        with self.sessions.begin() as session:
            event = self.app.state.record_event(
                session,
                claim_id=claim_id,
                event_type=event_type,
                payload=payload,
                actor=ACTOR,
                correlation_id=correlation_id,
            )
            return event.id

    def _open_card(self, claim_id: str) -> bool:
        with self.app.state.engine.connect() as connection:
            value = connection.execute(
                text(
                    "SELECT id FROM review_items WHERE claim_id = :claim_id "
                    "AND type = 'MODE_CONFIRM' AND status = 'open' "
                    "ORDER BY created_at, id LIMIT 1"
                ),
                {"claim_id": claim_id},
            ).scalar()
        return isinstance(value, str)

    @staticmethod
    def _json(value: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value

    def _event_payload(self, event_id: str, event_type: str) -> dict[str, Any]:
        with self.app.state.engine.connect() as connection:
            raw = connection.execute(
                text("SELECT payload FROM events WHERE id = :id AND type = :type"),
                {"id": event_id, "type": event_type},
            ).scalar()
        payload = self._json(raw)
        if not isinstance(payload, dict):
            raise LookupError(f"durable {event_type} trigger {event_id} was not found")
        return payload

    def _review_action(self, claim_id: str, review_id: str) -> dict[str, Any]:
        with self.app.state.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT payload FROM events WHERE claim_id = :claim_id "
                    "AND type = 'review.created' ORDER BY seq"
                ),
                {"claim_id": claim_id},
            )
        for row in rows:
            payload = self._json(row[0])
            if not isinstance(payload, dict) or payload.get("review_id") != review_id:
                continue
            action = payload.get("action")
            action_payload = action.get("payload") if isinstance(action, dict) else None
            if isinstance(action_payload, dict):
                return dict(action_payload)
        raise LookupError(f"mode card {review_id} has no durable staged action")

    @staticmethod
    def _as_datetime(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value.replace(tzinfo=UTC) if value.tzinfo is None else value
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
        return None

    def _shadow_spend(self, claim_id: str) -> tuple[Decimal, Decimal, Decimal]:
        now = self.app.state.clock()
        today = now.date()
        claim_daily = Decimal(0)
        claim_lifetime = Decimal(0)
        platform_daily = Decimal(0)
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
            payload = self._json(row["payload"])
            detail = payload.get("detail") if isinstance(payload, dict) else None
            if not isinstance(detail, dict) or detail.get("task") != self.shadow_config["purpose"]:
                continue
            try:
                cost = Decimal(str(detail.get("cost_usd")))
            except Exception:  # noqa: BLE001 - malformed historical telemetry is ignored
                continue
            occurred_at = self._as_datetime(row["occurred_at"])
            if row["claim_id"] == claim_id:
                claim_lifetime += cost
                if occurred_at is not None and occurred_at.date() == today:
                    claim_daily += cost
            if occurred_at is not None and occurred_at.date() == today:
                platform_daily += cost
        return claim_daily, claim_lifetime, platform_daily

    def _check_shadow_budget(
        self,
        claim_id: str,
        *,
        next_cost: Decimal = Decimal(0),
        before_call: bool = False,
    ) -> None:
        claim_daily, claim_lifetime, platform_daily = self._shadow_spend(claim_id)
        limits = {
            "claim_daily": Decimal(str(self.shadow_config["claim_daily_budget_usd"])),
            "claim_lifetime": Decimal(
                str(self.shadow_config["claim_lifetime_budget_usd"])
            ),
            "platform_daily": Decimal(
                str(self.shadow_config["platform_daily_budget_usd"])
            ),
        }
        used = {
            "claim_daily": claim_daily,
            "claim_lifetime": claim_lifetime,
            "platform_daily": platform_daily,
        }
        exceeded = [
            key
            for key in limits
            if (
                used[key] + next_cost >= limits[key]
                if before_call
                else used[key] + next_cost > limits[key]
            )
        ]
        if exceeded:
            raise ModelBudgetExceeded(
                "assessment shadow budget exceeded: " + ", ".join(sorted(exceeded))
            )

    def _budget_review(self, claim_id: str, run_id: str, error: Exception) -> str:
        review_id = new_ulid()
        self._emit(
            claim_id=claim_id,
            event_type="review.created",
            payload={
                "review_id": review_id,
                "type": "EXCEPTION",
                "subtype": "budget_exceeded",
                "agent_run_id": run_id,
                "capability_id": "assessment.mode_shadow",
                "facts": {"purpose": self.shadow_config["purpose"]},
                "risk": "the governed model budget cannot fund this shadow run",
                "recommendation": "review the model budget before unblocking the run",
                "resolution_schema": "EXCEPTION@1",
                "error_type": type(error).__name__,
            },
            correlation_id=run_id,
        )
        return review_id

    def _record_shadow_model_call(
        self,
        *,
        claim_id: str,
        run_id: str,
        cost_usd: float,
        model_id: str,
        status: str,
        error_type: str | None = None,
    ) -> None:
        detail: dict[str, Any] = {
            "claim_id": claim_id,
            "task": self.shadow_config["purpose"],
            "purpose": self.shadow_config["purpose"],
            "prompt_ref": self.shadow_config["prompt_ref"],
            "tier": self.shadow_config["tier"],
            "model_id": model_id,
            "cost_usd": cost_usd,
            "agent_run_id": run_id,
            "status": status,
        }
        if error_type is not None:
            detail["error_type"] = error_type
        self.app.state.claim_service.record_model_call(detail)

    def run_shadow(self, context: Any) -> dict[str, Any]:
        """Execute one recoverable Path-B step from durable card inputs."""

        if not isinstance(context.claim_id, str) or not isinstance(
            context.trigger_event, str
        ):
            raise ValueError("assessment shadow run requires claim and trigger event")
        trigger = self._event_payload(
            context.trigger_event, "assessment.mode_item_created"
        )
        mode_card_id = trigger.get("review_id")
        if not isinstance(mode_card_id, str):
            raise ValueError("assessment shadow trigger has no mode card id")
        action_payload = self._review_action(context.claim_id, mode_card_id)
        estimate_total = action_payload.get("estimate_total")
        estimate_document_id = action_payload.get("estimate_document_id")
        photos = action_payload.get("photos")
        if (
            not isinstance(estimate_total, int)
            or isinstance(estimate_total, bool)
            or (
                estimate_document_id is not None
                and not isinstance(estimate_document_id, str)
            )
            or not isinstance(photos, list)
            or not all(isinstance(photo, str) for photo in photos)
        ):
            raise ValueError("assessment shadow staged inputs are invalid")
        _claim, fields, _blocked = self.app.state.claim_service.hydrate_claim(
            context.claim_id, ACTOR, paths=["loss.narrative"]
        )
        narrative = fields.get("loss.narrative")
        inputs = {
            "task": self.shadow_config["purpose"],
            "purpose": self.shadow_config["purpose"],
            "prompt_ref": self.shadow_config["prompt_ref"],
            "estimate_total": estimate_total,
            "line_items_document_id": estimate_document_id,
            "damage_photo_document_ids": list(photos),
            "loss_narrative": narrative.value if narrative is not None else None,
            "vehicle_age": None,
        }
        wrapper: ModelWrapper | None = None
        call_recorded = False
        try:
            self._check_shadow_budget(context.claim_id, before_call=True)
            wrapper = ModelWrapper(
                self.model_client,
                budget_ceiling_usd=float(self.shadow_config["max_cost_usd"]),
                config={"tiers": self.shadow_config["tiers"]},
            )
            result = wrapper.structured_call(
                tier=self.shadow_config["tier"], schema=SHADOW_SCHEMA, inputs=inputs
            )
            cost = Decimal(str(wrapper.spent_usd))
            self._record_shadow_model_call(
                claim_id=context.claim_id,
                run_id=context.run_id,
                cost_usd=wrapper.spent_usd,
                model_id=result["model_id"],
                status="completed",
            )
            call_recorded = True
            if cost > Decimal(str(self.shadow_config["max_cost_usd"])):
                raise ModelBudgetExceeded("assessment shadow per-call budget exceeded")
            self._check_shadow_budget(context.claim_id)
            data = result.get("data") if isinstance(result, dict) else None
            if (
                not isinstance(data, dict)
                or data.get("mode") not in {"desk", "physical"}
                or not isinstance(data.get("confidence"), int | float)
                or isinstance(data.get("confidence"), bool)
            ):
                raise ValueError("assessment shadow returned an invalid structured result")
            log_payload = {
                "status": "completed",
                "mode_card_id": mode_card_id,
                "mode": data["mode"],
                "confidence": data["confidence"],
                "rationale": "__redacted__",
            }
        except ModelBudgetExceeded as error:
            if wrapper is not None and wrapper.spent_usd > 0 and not call_recorded:
                self._record_shadow_model_call(
                    claim_id=context.claim_id,
                    run_id=context.run_id,
                    cost_usd=wrapper.spent_usd,
                    model_id="unavailable",
                    status="failed",
                    error_type=type(error).__name__,
                )
            review_id = self._budget_review(context.claim_id, context.run_id, error)
            return {
                "status": "awaiting_review",
                "review_id": review_id,
                "log_payload": {
                    "status": "blocked",
                    "mode_card_id": mode_card_id,
                    "error_type": "budget_exceeded",
                    "rationale": "__redacted__",
                },
            }
        except Exception as error:  # Path B must not damage the governed Path A card.
            if wrapper is not None and wrapper.spent_usd > 0 and not call_recorded:
                self._record_shadow_model_call(
                    claim_id=context.claim_id,
                    run_id=context.run_id,
                    cost_usd=wrapper.spent_usd,
                    model_id="unavailable",
                    status="failed",
                    error_type=type(error).__name__,
                )
            log_payload = {
                "status": "failed",
                "mode_card_id": mode_card_id,
                "error_type": type(error).__name__,
                "rationale": "__redacted__",
            }
        return {"status": "completed", "log_payload": log_payload}

    def _start_shadow(self, claim_id: str, trigger_event: str) -> None:
        run_id = self.app.state.agent_runtime.start_run(
            agent="assessment",
            capability_id="assessment.mode_shadow",
            claim_id=claim_id,
            trigger_event=trigger_event,
        )
        self.app.state.agent_runtime.run(run_id)

    def issue_from_payload(
        self,
        *,
        claim_id: str,
        action_payload: dict[str, Any],
        retry_of: str | None = None,
    ) -> str | None:
        if self._open_card(claim_id):
            return None
        payload = dict(action_payload)
        if retry_of is not None:
            payload["retry_of"] = retry_of
        outcome = self.app.state.agent_runtime.execute_or_stage(
            capability_id="assessment.mode_confirm",
            action=Action(type="assessment.mode_confirm", payload=payload),
            claim_id=claim_id,
            actor=ACTOR,
        )
        review_id = outcome.get("review_id")
        if outcome.get("status") != "staged" or not isinstance(review_id, str):
            return None
        trigger_event = self._emit(
            claim_id=claim_id,
            event_type="assessment.mode_item_created",
            payload={
                "claim_id": claim_id,
                "review_id": review_id,
                "retry_of": retry_of,
            },
            correlation_id=review_id,
        )
        try:
            self._start_shadow(claim_id, trigger_event)
        except Exception:
            return review_id
        return review_id

    def _out_of_sequence(self, claim_id: str, source_event_id: str, status: str) -> None:
        self._emit(
            claim_id=claim_id,
            event_type="review.created",
            payload={
                "review_id": new_ulid(),
                "type": "EXCEPTION",
                "subtype": "assessment_out_of_sequence",
                "source_event_id": source_event_id,
                "facts": {"claim_status": status, "field_path": "assessment.estimate_total"},
                "risk": "assessment dispatch would bypass the canonical claim lifecycle",
                "recommendation": "repair the claim state before reprocessing the estimate",
                "resolution_schema": "EXCEPTION@1",
            },
            correlation_id=source_event_id,
        )

    def _process_estimate(
        self,
        *,
        claim_id: str,
        source_event_id: str,
        expected_field_id: str | None = None,
    ) -> None:
        claim, fields, _blocked = self.app.state.claim_service.hydrate_claim(
            claim_id, ACTOR, paths=["assessment.estimate_total"]
        )
        estimate = fields.get("assessment.estimate_total")
        if (
            estimate is None
            or (expected_field_id is not None and estimate.id != expected_field_id)
            or estimate.verification_state not in COMMITTED_STATES
            or not isinstance(estimate.value, int)
            or isinstance(estimate.value, bool)
        ):
            return
        metadata = STATE_METADATA[ClaimState(claim.status)]
        if metadata.suppresses_activity:
            return
        if claim.status == "INTIMATED":
            return
        if claim.status == "TRIAGED":
            self.app.state.claim_service.transition_claim(
                claim.id, "AWAITING_DOCS", {"trigger": "estimate_verified"}, ACTOR
            )
            self.app.state.claim_service.transition_claim(
                claim.id, "IN_ASSESSMENT", {"trigger": "estimate_received"}, ACTOR
            )
        elif claim.status == "AWAITING_DOCS":
            self.app.state.claim_service.transition_claim(
                claim.id, "IN_ASSESSMENT", {"trigger": "estimate_received"}, ACTOR
            )
        elif claim.status not in PROGRESSED_STATES:
            self._out_of_sequence(claim.id, source_event_id, claim.status)
            return
        if self._open_card(claim.id):
            return
        result = self.app.state.cop_runtime.evaluate("R-06", claim.id, ACTOR)
        source_ref = estimate.source_ref if isinstance(estimate.source_ref, dict) else {}
        estimate_document_id = source_ref.get("document_id")
        photos = sorted(
            document.id
            for document in self.app.state.claim_service.documents(claim.id)
            if document.doc_type == "photo_damage"
        )
        self.issue_from_payload(
            claim_id=claim.id,
            action_payload={
                "estimate_total": estimate.value,
                "rule": {
                    "rule_id": "R-06",
                    "status": result.status,
                    "rule_run_id": result.rule_run_id,
                    "verdict": "undetermined",
                },
                "photos": photos,
                "estimate_document_id": (
                    estimate_document_id if isinstance(estimate_document_id, str) else None
                ),
            },
        )

    def consume(self, event: Any) -> None:
        if not isinstance(event.claim_id, str) or not isinstance(event.payload, dict):
            return
        if (
            event.type == "field.updated"
            and event.payload.get("path") == "assessment.estimate_total"
        ):
            field_id = event.payload.get("field_id")
            if isinstance(field_id, str):
                self._process_estimate(
                    claim_id=event.claim_id,
                    source_event_id=event.id,
                    expected_field_id=field_id,
                )
            return
        if (
            event.type == "claim.status_changed"
            and event.payload.get("to") == "TRIAGED"
        ):
            self._process_estimate(
                claim_id=event.claim_id,
                source_event_id=event.id,
            )


__all__ = ["ACTOR", "AssessmentTrigger"]
