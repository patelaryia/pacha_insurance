"""Config-driven append-only PRD-01 cross-document consistency checks."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from rapidfuzz.fuzz import ratio


def load_definitions(path: str | Path | None = None) -> list[dict[str, Any]]:
    configured = Path(path) if path is not None else (
        Path(__file__).resolve().parents[2] / "packs" / "motor" / "consistency.yaml"
    )
    payload = yaml.safe_load(configured.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("checks"), list):
        raise ValueError("consistency config requires a checks list")
    return payload["checks"]


def _result(
    definition: Mapping[str, Any], status: str, rationale: str, *, evidence: dict[str, Any]
) -> dict[str, Any]:
    review = status != "consistent"
    return {
        "check_id": definition["id"],
        "status": status,
        "severity": definition["severity"],
        "rationale": rationale,
        "score": None,
        "evidence": evidence,
        "review_required": review,
        "review_type": "CONSISTENCY_FLAG" if review else None,
        "blocks": review and definition["severity"] == "block-pack",
    }


def evaluate_observations(
    observations: Mapping[str, Mapping[str, Any]],
    *,
    definitions: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Evaluate CC-1..CC-4 only when every configured trigger is present."""

    rows = []
    for definition in definitions or load_definitions():
        check_id = definition["id"]
        if check_id == "CC-5":
            continue
        triggers = definition["trigger_docs"]
        if any(trigger not in observations for trigger in triggers):
            continue
        expression = definition["expression"]
        if expression == "all_registrations_equal":
            values = {trigger: observations[trigger].get("reg") for trigger in triggers}
            complete = all(isinstance(value, str) and value.strip() for value in values.values())
            normalised = {value.upper().replace(" ", "") for value in values.values()}
            consistent = complete and len(normalised) == 1
            status = "consistent" if consistent else "inconsistent" if complete else "insufficient"
            rows.append(_result(definition, status, "registration comparison", evidence=values))
        elif expression == "driver_age_within_one_year":
            rows.append(
                _result(
                    definition,
                    "insufficient",
                    "claim-form driver age is not a registered extraction input",
                    evidence={"missing_input": "claim_form.driver_age"},
                )
            )
        elif expression == "licence_expiry_on_or_after_loss_date":
            expiry = observations["driving_licence"].get("expiry")
            loss_date = observations["claim"].get("loss_date")
            try:
                consistent = date.fromisoformat(str(expiry)) >= date.fromisoformat(str(loss_date))
                status = "consistent" if consistent else "inconsistent"
            except ValueError:
                status = "insufficient"
            rows.append(
                _result(
                    definition,
                    status,
                    "licence expiry compared with loss date",
                    evidence={"expiry": expiry, "loss_date": loss_date},
                )
            )
        elif expression == "owner_name_fuzzy_insured_name":
            owner = observations["logbook"].get("owner_name")
            insured = observations["claim"].get("insured_name")
            if not isinstance(owner, str) or not isinstance(insured, str):
                status = "insufficient"
                score = None
            else:
                score = ratio(owner.casefold(), insured.casefold()) / 100
                status = "consistent" if score >= float(definition["threshold"]) else "inconsistent"
            row = _result(
                definition,
                status,
                "owner and insured fuzzy-name comparison",
                evidence={"owner_name": owner, "insured_name": insured},
            )
            row["score"] = score
            rows.append(row)
        else:
            raise ValueError(f"unknown consistency expression {expression!r}")
    return rows


def evaluate_cc5(
    *, narrative: str, photo_descriptions: list[str], model_client: Any
) -> dict[str, Any]:
    result = model_client.structured_call(
        tier="MODEL_HEAVY",
        schema={
            "type": "object",
            "required": ["status", "rationale", "score"],
            "additionalProperties": False,
            "properties": {
                "status": {"enum": ["consistent", "inconsistent", "insufficient"]},
                "rationale": {"type": "string"},
                "score": {"type": "number", "minimum": 0, "maximum": 1},
            },
        },
        inputs={
            "task": "consistency_cc5",
            "narrative": narrative,
            "photo_descriptions": photo_descriptions,
        },
    )
    data = dict(result["data"])
    data.update(
        {
            "check_id": "CC-5",
            "severity": "flag",
            "review_required": data["status"] != "consistent",
            "review_type": "CONSISTENCY_FLAG" if data["status"] != "consistent" else None,
            "blocks": False,
            "auto_clear": False,
            "max_autonomy_level": "L2",
            "cost_usd": float(result["cost_usd"]),
        }
    )
    return data
