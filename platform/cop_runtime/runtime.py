"""Public COP runtime wiring, evaluation, recording, and pack pinning."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from claim_core import Base, new_ulid
from cop_runtime.calcs import CalcInputsMissing, CalcRegistry, CalcResult
from cop_runtime.models import CalcRun, RuleRun
from cop_runtime.money import Money
from cop_runtime.pack_loader import (
    LoadedPack,
    PackLoadError,
    load_pack,
    peek_pack_identity,
)
from cop_runtime.rules import InputBinding, RuleRegistry, RuleResult, evaluate_logic

COMMITTED_STATES = frozenset({"extracted", "human_verified", "system_confirmed"})
VERIFICATION_RANK = {"extracted": 0, "system_confirmed": 1, "human_verified": 2}


class CopRuntime:
    """Generic runtime for version-pinned pack rules and calculations."""

    def __init__(self, app: Any) -> None:
        self._app = app
        self._engine = app.state.engine
        self._claim_service = app.state.claim_service
        self._sessions = sessionmaker(bind=self._engine, expire_on_commit=False)
        self._packs: dict[tuple[str, str], LoadedPack] = {}
        Base.metadata.create_all(
            self._engine,
            tables=[RuleRun.__table__, CalcRun.__table__],
        )

    def load_pack(self, path: str | Path) -> LoadedPack:
        """Load one pack version without replacing an existing registration."""

        key = peek_pack_identity(path)
        if key in self._packs:
            raise PackLoadError(f"Pack {key[0]}@{key[1]} is already loaded")
        loaded = load_pack(path)
        self._packs[key] = loaded
        return loaded

    def _pack(self, pack_id: str, version: str) -> LoadedPack:
        try:
            return self._packs[(pack_id, version)]
        except KeyError as error:
            raise LookupError(
                f"PACK_VERSION_NOT_LOADED: {pack_id}@{version}"
            ) from error

    def rule_registry(self, pack_id: str, version: str) -> RuleRegistry:
        """Return rules for one loaded pack version."""

        return self._pack(pack_id, version).rule_registry

    def calc_registry(self, pack_id: str, version: str) -> CalcRegistry:
        """Return calculations for one loaded pack version."""

        return self._pack(pack_id, version).calc_registry

    def _claim_context(self, claim_id: str, actor: str) -> tuple[Any, dict, LoadedPack]:
        claim, fields, _blocked_reasons = self._claim_service.hydrate_claim(
            claim_id, actor
        )
        pack_id, separator, version = claim.pack_version.partition("@")
        if not separator or not pack_id or not version:
            raise LookupError(
                f"PACK_VERSION_NOT_LOADED: malformed pin {claim.pack_version!r}"
            )
        return claim, fields, self._pack(pack_id, version)

    @staticmethod
    def _verification_qualifies(state: str, minimum: str | None) -> bool:
        if state not in COMMITTED_STATES:
            return False
        if minimum is None:
            return True
        return VERIFICATION_RANK[state] >= VERIFICATION_RANK[minimum]

    @staticmethod
    def _normalise_field_value(field: Any) -> Any:
        if field.value_type == "date":
            parsed = date.fromisoformat(field.value)
            return (parsed - date(1970, 1, 1)).days
        if field.value_type == "datetime":
            parsed = datetime.fromisoformat(field.value.replace("Z", "+00:00"))
            return (parsed.astimezone(UTC).date() - date(1970, 1, 1)).days
        return field.value

    def _latest_calc_run(self, claim_id: str, calc_id: str) -> str | None:
        with self._sessions() as session:
            return session.scalar(
                select(CalcRun.id)
                .where(
                    CalcRun.claim_id == claim_id,
                    CalcRun.calc_id == calc_id,
                    CalcRun.status == "executed",
                )
                .order_by(CalcRun.ts.desc(), CalcRun.id.desc())
                .limit(1)
            )

    def _resolve_binding(
        self,
        binding: InputBinding,
        *,
        claim_id: str,
        actor: str,
        fields: dict[str, Any],
        pack: LoadedPack,
    ) -> tuple[bool, Any]:
        path = binding.path
        if path == "runtime.routing_amount":
            amount = self.routing_amount(claim_id, actor)
            return amount is not None, amount
        latest_prefix = "runtime.latest_calc_run."
        if path.startswith(latest_prefix):
            run_id = self._latest_calc_run(claim_id, path.removeprefix(latest_prefix))
            return run_id is not None, run_id
        if path.startswith("pack."):
            key = path.removeprefix("pack.")
            if key not in pack.config:
                return False, None
            value = pack.config[key]
            if isinstance(value, dict) and "status" in value:
                if value.get("status") == "blocked_on_inputs":
                    return False, None
                value = value.get("value")
            return value is not None, value
        field = fields.get(path)
        if field is None or not self._verification_qualifies(
            field.verification_state, binding.min_verification
        ):
            return False, None
        return True, self._normalise_field_value(field)

    def _bind(
        self,
        bindings: dict[str, InputBinding],
        *,
        claim_id: str,
        actor: str,
        fields: dict[str, Any],
        pack: LoadedPack,
        optional: bool = False,
    ) -> tuple[dict[str, Any], list[str]]:
        values = {}
        missing = []
        for alias, binding in bindings.items():
            found, value = self._resolve_binding(
                binding,
                claim_id=claim_id,
                actor=actor,
                fields=fields,
                pack=pack,
            )
            if found:
                values[alias] = value
            elif not optional:
                missing.append(binding.path)
        return values, missing

    def _record_rule_run(
        self,
        *,
        claim_id: str,
        actor: str,
        pack: LoadedPack,
        rule_id: str,
        rule_version: str,
        status: str,
        fired: bool | None,
        outcome: dict[str, Any] | None,
        inputs_snapshot: dict[str, Any],
        missing_inputs: list[str],
    ) -> None:
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            session.add(
                RuleRun(
                    id=new_ulid(),
                    claim_id=claim_id,
                    rule_id=rule_id,
                    rule_version=rule_version,
                    pack_id=pack.pack_id,
                    pack_version=pack.version,
                    status=status,
                    fired=fired,
                    outcome=outcome,
                    inputs_snapshot=inputs_snapshot,
                    missing_inputs=missing_inputs,
                    actor=actor,
                    evaluated_at=now,
                )
            )
            self._claim_service.record_event(
                session,
                claim_id=claim_id,
                event_type="rule.evaluated",
                payload={
                    "rule_id": rule_id,
                    "rule_version": rule_version,
                    "pack": f"{pack.pack_id}@{pack.version}",
                    "status": status,
                    "fired": fired,
                },
                actor=actor,
                correlation_id=new_ulid(),
            )

    def evaluate(self, rule_id: str, claim_id: str, actor: str) -> RuleResult:
        """Evaluate and atomically record one rule without executing its outcome."""

        _claim, fields, pack = self._claim_context(claim_id, actor)
        definition = pack.rule_registry.get(rule_id)
        inputs_snapshot: dict[str, Any] = {}
        if definition.status == "blocked_on_inputs":
            status = "blocked_on_inputs"
            fired = None
            outcome = None
            missing_inputs = list(definition.blocked_on)
        else:
            inputs_snapshot, missing_inputs = self._bind(
                definition.inputs,
                claim_id=claim_id,
                actor=actor,
                fields=fields,
                pack=pack,
            )
            if missing_inputs:
                status = "blocked_on_inputs"
                fired = None
                outcome = None
            else:
                status = "evaluated"
                fired = evaluate_logic(definition, inputs_snapshot)
                outcome = definition.outcome if fired else None
        self._record_rule_run(
            claim_id=claim_id,
            actor=actor,
            pack=pack,
            rule_id=definition.rule_id,
            rule_version=definition.version,
            status=status,
            fired=fired,
            outcome=outcome,
            inputs_snapshot=inputs_snapshot,
            missing_inputs=missing_inputs,
        )
        return RuleResult(
            rule_id=definition.rule_id,
            rule_version=definition.version,
            pack_id=pack.pack_id,
            pack_version=pack.version,
            status=status,
            fired=fired,
            outcome=outcome,
            inputs_snapshot=inputs_snapshot,
            missing_inputs=missing_inputs,
        )

    def _record_calc_run(
        self,
        *,
        claim_id: str,
        actor: str,
        pack: LoadedPack,
        calc_id: str,
        version: str,
        status: str,
        inputs: dict[str, Any],
        output: Any | None,
        missing_inputs: list[str],
    ) -> None:
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            session.add(
                CalcRun(
                    id=new_ulid(),
                    calc_id=calc_id,
                    version=version,
                    inputs=inputs,
                    output=output,
                    claim_id=claim_id,
                    ts=now,
                    pack_id=pack.pack_id,
                    pack_version=pack.version,
                    status=status,
                    missing_inputs=missing_inputs,
                    actor=actor,
                )
            )
            self._claim_service.record_event(
                session,
                claim_id=claim_id,
                event_type="calc.executed",
                payload={
                    "calc_id": calc_id,
                    "calc_version": version,
                    "pack": f"{pack.pack_id}@{pack.version}",
                    "status": status,
                },
                actor=actor,
                correlation_id=new_ulid(),
            )

    def execute_calc(self, calc_id: str, claim_id: str, actor: str) -> CalcResult:
        """Bind, execute, and atomically record one pure pack calculation."""

        _claim, fields, pack = self._claim_context(claim_id, actor)
        definition = pack.calc_registry.get(calc_id)
        inputs: dict[str, Any] = {}
        if definition.status == "blocked_on_inputs":
            status = "blocked_on_inputs"
            output = None
            missing_inputs = list(definition.blocked_on)
        else:
            bindings = {
                alias: InputBinding(path) for alias, path in definition.inputs.items()
            }
            inputs, missing_inputs = self._bind(
                bindings,
                claim_id=claim_id,
                actor=actor,
                fields=fields,
                pack=pack,
            )
            optional_bindings = {
                alias: InputBinding(path)
                for alias, path in definition.optional_inputs.items()
            }
            optional_inputs, _ = self._bind(
                optional_bindings,
                claim_id=claim_id,
                actor=actor,
                fields=fields,
                pack=pack,
                optional=True,
            )
            inputs.update(optional_inputs)
            if missing_inputs:
                status = "blocked_on_inputs"
                output = None
            else:
                try:
                    output = definition.function(**inputs)
                except CalcInputsMissing as error:
                    status = "blocked_on_inputs"
                    output = None
                    missing_inputs = error.missing_inputs
                else:
                    status = "executed"
        self._record_calc_run(
            claim_id=claim_id,
            actor=actor,
            pack=pack,
            calc_id=definition.calc_id,
            version=definition.version,
            status=status,
            inputs=inputs,
            output=output,
            missing_inputs=missing_inputs,
        )
        return CalcResult(
            calc_id=definition.calc_id,
            calc_version=definition.version,
            pack_id=pack.pack_id,
            pack_version=pack.version,
            status=status,
            output=output,
            inputs=inputs,
            missing_inputs=missing_inputs,
        )

    def routing_amount(self, claim_id: str, actor: str) -> Money | None:
        """Return payable when live, otherwise the configured safe reserve fallback."""

        _claim, fields, pack = self._claim_context(claim_id, actor)
        calc_id = pack.config.get("routing_amount_calc")
        if isinstance(calc_id, str):
            definition = pack.calc_registry.get(calc_id)
            if definition.status == "live":
                result = self.execute_calc(calc_id, claim_id, actor)
                if result.status == "executed" and isinstance(result.output, int):
                    return Money(result.output)
        fallback_path = pack.config.get("routing_amount_fallback")
        fallback = fields.get(fallback_path) if isinstance(fallback_path, str) else None
        if (
            fallback is None
            or not self._verification_qualifies(fallback.verification_state, None)
            or not isinstance(fallback.value, int)
            or isinstance(fallback.value, bool)
        ):
            return None
        return Money(fallback.value)


def build_cop_runtime(app: Any, *, pack_paths: list[str | Path]) -> CopRuntime:
    """Build the runtime, load all requested packs, and expose it on app state."""

    runtime = CopRuntime(app)
    for path in pack_paths:
        runtime.load_pack(path)
    app.state.cop_runtime = runtime
    return runtime


__all__ = ["CopRuntime", "build_cop_runtime"]
