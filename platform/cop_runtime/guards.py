"""COP rule hooks for the three rule-linked FSM edges in PACKET-07."""

from __future__ import annotations

from typing import Any

from claim_core import ClaimState

GUARD_ACTOR = "agent:cop"


def _requires_fired(runtime: Any, rule_id: str, claim_id: str) -> dict[str, Any]:
    result = runtime.evaluate(rule_id, claim_id, actor=GUARD_ACTOR)
    passed = result.status == "evaluated" and result.fired is True
    if passed:
        blocked_on: list[str] = []
    elif result.status != "evaluated":
        detail = ", ".join(result.missing_inputs)
        blocked_on = [f"{rule_id} {result.status}: {detail}"]
    else:
        blocked_on = [f"{rule_id} evaluated not-fired"]
    return {"passed": passed, "blocked_on": blocked_on}


def _settlement_clear(runtime: Any, claim_id: str) -> dict[str, Any]:
    blocked_on = []
    for rule_id in ("R-13", "R-14"):
        result = runtime.evaluate(rule_id, claim_id, actor=GUARD_ACTOR)
        if result.status != "evaluated":
            detail = ", ".join(result.missing_inputs)
            blocked_on.append(f"{rule_id} {result.status}: {detail}")
        elif result.fired is not False:
            blocked_on.append(f"{rule_id} fired")
    return {"passed": not blocked_on, "blocked_on": blocked_on}


def register_cop_guards(runtime: Any) -> None:
    """Wire the fixed PRD-02 rules into the claim FSM's hook registry."""

    fsm = runtime._claim_service.fsm
    fsm.register_guard_hook(
        (ClaimState.REPORT_RECEIVED, ClaimState.WRITE_OFF),
        lambda claim_id: _requires_fired(runtime, "R-05", claim_id),
    )
    fsm.register_guard_hook(
        (ClaimState.IN_REPAIR, ClaimState.REINSPECTION),
        lambda claim_id: _requires_fired(runtime, "R-08", claim_id),
    )
    fsm.register_guard_hook(
        (ClaimState.SURRENDER_CHECKLIST, ClaimState.SETTLEMENT),
        lambda claim_id: _settlement_clear(runtime, claim_id),
    )


__all__ = ["register_cop_guards"]
