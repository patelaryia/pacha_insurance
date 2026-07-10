"""PRD-01 confidence combination with data-defined thresholds."""

from __future__ import annotations

from decimal import Decimal

from doc_intel.settings import DEFAULTS

VALIDATOR_MULTIPLIERS = {
    "pass": Decimal("1.0"),
    "fail": Decimal("0.30"),
    "not_applicable": Decimal("0.95"),
    "out_of_scope": Decimal("0.30"),
}

DEFAULT_THRESHOLDS = {
    name: Decimal(str(value))
    for name, value in DEFAULTS["confidence_thresholds"].items()
}


def combined_confidence(model_confidence: float, validator_outcome: str) -> Decimal:
    """Return model confidence multiplied by the binding validator factor."""

    try:
        multiplier = VALIDATOR_MULTIPLIERS[validator_outcome]
    except KeyError as error:
        raise ValueError(f"unknown validator outcome {validator_outcome!r}") from error
    return Decimal(str(model_confidence)) * multiplier


def threshold_for(field_schema: dict) -> Decimal:
    """Resolve an explicit schema override or the PRD-01 category default."""

    explicit = field_schema.get("confidence_threshold")
    if explicit is not None:
        return Decimal(str(explicit))
    validator = field_schema.get("validator")
    if validator == "money_kes":
        category = "money"
    elif validator == "date_past":
        category = "date"
    elif validator == "kenya_reg":
        category = "registration"
    else:
        category = "other"
    return DEFAULT_THRESHOLDS[category]
