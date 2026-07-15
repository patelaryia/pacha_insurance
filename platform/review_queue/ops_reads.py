"""Read models for the PACKET-12 S-4, S-5, and S-6 operations surfaces."""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from sqlalchemy import text

from claim_core import ClaimCoreError

LIVE_SERIES = frozenset(
    {
        "open_claims_by_state",
        "sla_breaches",
        "per_officer_queue_depth",
        "aging_histogram",
        "savings_mtd_ytd",
    }
)


def _aware(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class OpsReadService:
    """Database-backed ops reads with config-owned series definitions."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.repo = Path(__file__).resolve().parents[2]
        try:
            config = yaml.safe_load(
                (self.repo / "packs/motor/dashboard.yaml").read_text(encoding="utf-8")
            )
        except (OSError, yaml.YAMLError) as error:
            raise ValueError(f"invalid dashboard config: {error}") from error
        if not isinstance(config, dict) or config.get("version") != 1:
            raise ValueError("dashboard config requires version 1")
        series = config.get("series")
        buckets = config.get("aging_buckets")
        if not isinstance(series, list) or not isinstance(buckets, list):
            raise ValueError("dashboard config requires series and aging buckets")
        self.config = config
        self.series = {row["id"]: dict(row) for row in series}
        if set(self.series) & LIVE_SERIES != LIVE_SERIES:
            raise ValueError("dashboard config omits a required live series")

    def sla_board(self) -> list[dict[str, Any]]:
        with self.app.state.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT c.id AS clock_id, c.claim_id, c.definition_id, c.state, "
                    "c.started_at, c.breach_at, "
                    "COALESCE(d.escalate_to_role, 'pending_capture') AS escalate_to_role "
                    "FROM sla_clocks c LEFT JOIN sla_definitions d "
                    "ON d.id = c.definition_id WHERE c.stopped_at IS NULL "
                    "ORDER BY CASE WHEN c.breach_at IS NULL THEN 1 ELSE 0 END, "
                    "c.breach_at, c.id"
                )
            ).mappings()
            return [dict(row) for row in rows]

    def _open_claims_by_state(self) -> list[dict[str, Any]]:
        with self.app.state.engine.connect() as connection:
            rows = connection.execute(
                text("SELECT status, COUNT(*) FROM claims GROUP BY status ORDER BY status")
            )
            return [{"state": state, "count": int(count)} for state, count in rows]

    def _sla_breaches(self) -> list[dict[str, Any]]:
        now = self.app.state.clock()
        with self.app.state.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id AS clock_id, claim_id, definition_id, state, breach_at "
                    "FROM sla_clocks WHERE stopped_at IS NULL "
                    "AND (state = 'breached' OR (breach_at IS NOT NULL AND breach_at <= :now)) "
                    "ORDER BY breach_at, id"
                ),
                {"now": now},
            ).mappings()
            return [dict(row) for row in rows]

    def _queue_depth(self) -> list[dict[str, Any]]:
        with self.app.state.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT assigned_to, COUNT(*) FROM review_items "
                    "WHERE status = 'open' GROUP BY assigned_to ORDER BY assigned_to"
                )
            )
            return [
                {"assigned_to": actor, "count": int(count)} for actor, count in rows
            ]

    def _aging_histogram(self) -> list[dict[str, Any]]:
        now = _aware(self.app.state.clock())
        with self.app.state.engine.connect() as connection:
            created = [
                _aware(value)
                for value in connection.execute(
                    text("SELECT created_at FROM claims")
                ).scalars()
            ]
        ages = [max(0, (now - value).days) for value in created]
        rows = []
        for bucket in self.config["aging_buckets"]:
            lower = int(bucket["min_days"])
            upper = bucket.get("max_days")
            count = sum(
                age >= lower and (upper is None or age <= int(upper)) for age in ages
            )
            rows.append({"bucket": bucket["id"], "count": count})
        return rows

    @staticmethod
    def _savings_value(output: Any) -> int | None:
        if isinstance(output, int) and not isinstance(output, bool):
            return output
        if isinstance(output, dict):
            value = output.get("savings")
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        return None

    def _savings(self) -> dict[str, int]:
        timezone = ZoneInfo(self.config["timezone"])
        now_eat = _aware(self.app.state.clock()).astimezone(timezone)
        mtd = 0
        ytd = 0
        with self.app.state.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT output, ts FROM calc_runs "
                    "WHERE calc_id = 'C-05' AND status = 'executed'"
                )
            )
            for output, occurred_at in rows:
                if isinstance(output, str):
                    try:
                        output = json.loads(output)
                    except json.JSONDecodeError:
                        continue
                value = self._savings_value(output)
                if value is None:
                    continue
                occurred_eat = _aware(occurred_at).astimezone(timezone)
                if occurred_eat.year == now_eat.year:
                    ytd += value
                    if occurred_eat.month == now_eat.month:
                        mtd += value
        return {"mtd": mtd, "ytd": ytd}

    def series_data(self, series_id: str) -> Any:
        readers = {
            "open_claims_by_state": self._open_claims_by_state,
            "sla_breaches": self._sla_breaches,
            "per_officer_queue_depth": self._queue_depth,
            "aging_histogram": self._aging_histogram,
            "savings_mtd_ytd": self._savings,
        }
        reader = readers.get(series_id)
        return None if reader is None else reader()

    def portfolio(self) -> list[dict[str, Any]]:
        tiles = []
        for series_id, config in self.series.items():
            status = config["status"]
            tiles.append(
                {
                    "series_id": series_id,
                    "status": status,
                    "data": self.series_data(series_id) if status == "live" else None,
                }
            )
        return tiles

    def csv(self, series_id: str) -> str:
        config = self.series.get(series_id)
        if config is None:
            raise ClaimCoreError(404, "SERIES_NOT_FOUND", "Series was not found")
        if config.get("status") != "live":
            raise ClaimCoreError(
                409,
                "SERIES_BLOCKED_ON_INPUTS",
                "Series window or denominator is pending capture",
            )
        data = self.series_data(series_id)
        if isinstance(data, dict):
            rows = [{"key": key, "value": value} for key, value in data.items()]
        elif isinstance(data, list):
            rows = data
        else:
            rows = [{"value": data}]
        headers: list[str] = []
        for row in rows:
            for key in row:
                if key not in headers:
                    headers.append(key)
        if not headers:
            headers = ["value"]
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return stream.getvalue()

    def ledger(
        self,
        *,
        actor: str | None,
        action: str | None,
        claim_id: str | None,
        after_seq: int | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        clauses = []
        params: dict[str, Any] = {"limit": limit}
        for key, value in (("actor", actor), ("action", action), ("claim_id", claim_id)):
            if value is not None:
                clauses.append(f"{key} = :{key}")
                params[key] = value
        if after_seq is not None:
            clauses.append("seq > :after_seq")
            params["after_seq"] = after_seq
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.app.state.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, seq, occurred_at, actor, action, claim_id, object_ref, "
                    "before_hash, after_hash, detail, row_hash FROM audit_ledger "
                    f"{where} ORDER BY seq LIMIT :limit"
                ),
                params,
            ).mappings()
            return [dict(row) for row in rows]

    def packs(self) -> dict[str, Any]:
        with self.app.state.engine.connect() as connection:
            pins = [
                value
                for value in connection.execute(
                    text("SELECT DISTINCT pack_version FROM claims ORDER BY pack_version")
                ).scalars()
                if isinstance(value, str) and "@" in value
            ]
        runtime = self.app.state.cop_runtime
        packs = []
        for pin in pins:
            pack_id, version = pin.split("@", 1)
            rule_registry = runtime.rule_registry(pack_id, version)
            calc_registry = runtime.calc_registry(pack_id, version)
            template_registry = runtime.template_registry(pack_id, version)
            entries = [
                {
                    "kind": "rule",
                    "id": item_id,
                    "version": rule_registry.get(item_id).version,
                    "status": rule_registry.get(item_id).status,
                }
                for item_id in rule_registry.ids()
            ]
            entries.extend(
                {
                    "kind": "calc",
                    "id": item_id,
                    "version": calc_registry.get(item_id).version,
                    "status": calc_registry.get(item_id).status,
                }
                for item_id in calc_registry.ids()
            )
            entries.extend(
                {
                    "kind": "template",
                    "id": item_id,
                    "version": template_registry.get(item_id).version,
                    "status": template_registry.get(item_id).status,
                }
                for item_id in template_registry.ids()
            )
            packs.append({"id": pack_id, "version": pin, "entries": entries})
        roles = dict(
            getattr(
                self.app.state,
                "console_roles",
                self.app.state.review_queue.roles,
            )
        )
        return {
            "packs": packs,
            "adapter_health": {"status": "unavailable", "owner": "PRD-09"},
            "user_roles": {
                "status": "config-managed",
                "provenance": "packs/motor/routing/roles.yaml",
                "assignment_count": len(roles),
                "assignments": [
                    {"actor": actor, "role": role}
                    for actor, role in sorted(roles.items())
                ],
            },
        }

    def capabilities(self) -> list[dict[str, Any]]:
        harness = self.app.state.eval_harness
        with self.app.state.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, current_level, max_level, policy "
                    "FROM capabilities ORDER BY id"
                )
            ).mappings()
            result = []
            for row in rows:
                policy = row["policy"]
                if isinstance(policy, str):
                    policy = json.loads(policy)
                evidence = harness.autonomy.evidence(row["id"])
                result.append(
                    {
                        "id": row["id"],
                        "current_level": row["current_level"],
                        "max_level": row["max_level"],
                        "pass_rate_window": evidence["grader_pass_percent"],
                        "consecutive_approvals": evidence["consecutive_approvals"],
                        "runs_to_promotion": None,
                        "sampling_rate": int(policy.get("sampling_rate", 0)),
                        "promotion_evidence": evidence,
                    }
                )
        return result


__all__ = ["OpsReadService"]
