"""Pure, table-driven PRD-01 extraction validators."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

ValidatorOutcome = Literal["pass", "fail", "not_applicable", "out_of_scope"]


@dataclass(frozen=True)
class ValidatorResult:
    outcome: ValidatorOutcome
    value: Any


KENYA_REG_PATTERNS = (
    re.compile(r"^K[A-Z]{2} ?\d{3}[A-Z]$"),
    re.compile(r"^K[A-Z]{2} ?\d{3}$"),
    re.compile(r"^KM[A-Z]{2} ?\d{3}[A-Z]$"),
    re.compile(r"^Z[A-Z] ?\d{4}$"),
    re.compile(r"^GK ?[A-Z]? ?\d{3}[A-Z]?$"),
)
KRA_PIN_PATTERN = re.compile(r"^[AP]\d{9}[A-Z]$")


def _empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def kenya_reg(value: Any) -> ValidatorResult:
    if _empty(value):
        return ValidatorResult("not_applicable", value)
    if not isinstance(value, str):
        return ValidatorResult("out_of_scope", value)
    normalised = " ".join(value.upper().split())
    if any(pattern.fullmatch(normalised) for pattern in KENYA_REG_PATTERNS):
        return ValidatorResult("pass", normalised)
    return ValidatorResult("out_of_scope", normalised)


def kra_pin(value: Any) -> ValidatorResult:
    if _empty(value):
        return ValidatorResult("not_applicable", value)
    if not isinstance(value, str):
        return ValidatorResult("fail", value)
    normalised = re.sub(r"\s+", "", value).upper()
    return ValidatorResult(
        "pass" if KRA_PIN_PATTERN.fullmatch(normalised) else "fail", normalised
    )


def date_past(value: Any, *, today: date | None = None) -> ValidatorResult:
    if _empty(value):
        return ValidatorResult("not_applicable", value)
    if isinstance(value, datetime):
        parsed = value.date()
    elif isinstance(value, date):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = date.fromisoformat(value.strip())
        except ValueError:
            return ValidatorResult("fail", value)
    else:
        return ValidatorResult("fail", value)
    reference = today or datetime.now(UTC).date()
    return ValidatorResult(
        "pass" if parsed <= reference else "fail", parsed.isoformat()
    )


def money_kes(value: Any) -> ValidatorResult:
    """Parse one shilling-denominated amount to integer KES cents."""

    if _empty(value):
        return ValidatorResult("not_applicable", value)
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        return ValidatorResult("fail", value)
    if isinstance(value, int):
        amount = Decimal(value)
    elif isinstance(value, Decimal):
        amount = value
    else:
        candidate = value.strip()
        candidate = re.sub(r"(?i)\b(?:KES|KSH)\.?\s*", "", candidate)
        candidate = candidate.replace(",", "").strip()
        if not re.fullmatch(r"-?\d+(?:\.\d{1,2})?", candidate):
            return ValidatorResult("fail", value)
        try:
            amount = Decimal(candidate)
        except InvalidOperation:
            return ValidatorResult("fail", value)
    cents = amount * 100
    if cents != cents.to_integral_value():
        return ValidatorResult("fail", value)
    return ValidatorResult("pass", int(cents))


def sum_check(line_amounts: Any, total: Any) -> ValidatorResult:
    if not isinstance(line_amounts, list):
        return ValidatorResult("fail", total)
    if isinstance(total, bool) or not isinstance(total, int):
        return ValidatorResult("fail", total)
    amounts: list[int] = []
    for line in line_amounts:
        amount = line.get("amount") if isinstance(line, dict) else line
        if isinstance(amount, bool) or not isinstance(amount, int):
            return ValidatorResult("fail", total)
        amounts.append(amount)
    return ValidatorResult(
        "pass" if abs(sum(amounts) - total) <= 1_00 else "fail", total
    )


def licence_no(value: Any) -> ValidatorResult:
    """Conservatively route non-empty licences to review until a pattern is captured."""

    if _empty(value):
        return ValidatorResult("not_applicable", value)
    normalised = " ".join(str(value).upper().split())
    return ValidatorResult("out_of_scope", normalised)


def phone_ke(value: Any) -> ValidatorResult:
    if _empty(value):
        return ValidatorResult("not_applicable", value)
    digits = re.sub(r"\D", "", str(value))
    if digits.startswith("0"):
        digits = f"254{digits[1:]}"
    elif len(digits) == 9 and digits.startswith(("1", "7")):
        digits = f"254{digits}"
    return ValidatorResult("out_of_scope", digits)


def not_applicable(value: Any) -> ValidatorResult:
    return ValidatorResult("not_applicable", value)


VALIDATORS = {
    "kenya_reg": kenya_reg,
    "kra_pin": kra_pin,
    "date_past": date_past,
    "money_kes": money_kes,
    "licence_no": licence_no,
    "phone_ke": phone_ke,
    "not_applicable": not_applicable,
}


def validate_field(
    name: str, value: Any, *, today: date | None = None
) -> ValidatorResult:
    try:
        validator = VALIDATORS[name]
    except KeyError as error:
        raise ValueError(f"unknown validator {name!r}") from error
    if name == "date_past":
        return date_past(value, today=today)
    return validator(value)
