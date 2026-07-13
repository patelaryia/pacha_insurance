"""Motor v1 calculations. This is the pack's sole executable module."""

from decimal import Decimal

from cop_runtime.calcs import CalcInputsMissing, calc
from cop_runtime.money import Money


@calc("C-01", version="1.0.0", inputs={"sum_insured": "policy.sum_insured"})
def excess(sum_insured: Money) -> Money:
    """Return the policy excess, clamped to the captured motor bounds."""

    percentage = int(round(Decimal(sum_insured) * Decimal(25) / Decimal(1000)))
    return Money(max(15_000_00, min(percentage, 100_000_00)))


@calc(
    "C-02",
    version="1.0.0",
    inputs={
        "agreed_quote": "assessment.agreed_quote",
        "assessor_fee": "assessment.assessor_fee",
        "reinspection_fee": "assessment.reinspection_fee",
    },
)
def reserve(
    agreed_quote: Money, assessor_fee: Money, reinspection_fee: Money
) -> Money:
    """Return the total repair reserve."""

    return Money(agreed_quote + assessor_fee + reinspection_fee)


@calc(
    "C-03",
    version="1.0.0",
    inputs={
        "agreed_quote": "assessment.agreed_quote",
        "assessor_fee": "assessment.assessor_fee",
        "reinspection_fee": "assessment.reinspection_fee",
        "garage_party_id": "assessment.garage_party_id",
        "assessor_party_id": "assessment.assessor_party_id",
        "supplier_lines": "assessment.supplier_lines",
        "parent_reserve_id": "runtime.latest_calc_run.C-02",
    },
)
def reserve_breakdown(
    agreed_quote: Money,
    assessor_fee: Money,
    reinspection_fee: Money,
    garage_party_id: str,
    assessor_party_id: str,
    supplier_lines: dict,
    parent_reserve_id: str,
) -> list[dict]:
    """Return linked reserve lines whose amounts exactly reconstruct C-02."""

    if "lines" not in supplier_lines or not isinstance(supplier_lines["lines"], list):
        raise CalcInputsMissing(["assessment.supplier_lines.lines"])
    raw_lines = supplier_lines["lines"]
    lines = []
    supplier_total = 0
    for supplier in raw_lines:
        if (
            not isinstance(supplier, dict)
            or "payee_party_id" not in supplier
            or "amount" not in supplier
            or not isinstance(supplier["amount"], int)
            or isinstance(supplier["amount"], bool)
        ):
            raise CalcInputsMissing(
                [
                    "assessment.supplier_lines.lines[].payee_party_id",
                    "assessment.supplier_lines.lines[].amount",
                ]
            )
        amount = supplier["amount"]
        supplier_total += amount
        lines += [
            {
                "category": "supplier",
                "payee_party_id": supplier["payee_party_id"],
                "amount": amount,
                "parent_reserve_id": parent_reserve_id,
            }
        ]
    lines += [
        {
            "category": "garage_residual",
            "payee_party_id": garage_party_id,
            "amount": agreed_quote - supplier_total,
            "parent_reserve_id": parent_reserve_id,
        },
        {
            "category": "assessor",
            "payee_party_id": assessor_party_id,
            "amount": assessor_fee,
            "parent_reserve_id": parent_reserve_id,
        },
        {
            "category": "reinspection_residual",
            "payee_party_id": assessor_party_id,
            "amount": reinspection_fee,
            "parent_reserve_id": parent_reserve_id,
        },
    ]
    expected = agreed_quote + assessor_fee + reinspection_fee
    assert sum(line["amount"] for line in lines) == expected
    return lines


@calc(
    "C-04",
    version="1.0.0",
    status="blocked_on_inputs",
    blocked_on=("formula.C-04",),
)
def pending_c04() -> None:
    """Keep the uncaptured C-04 slot visible without producing a value."""

    return None


@calc(
    "C-05",
    version="1.0.0",
    inputs={
        "estimate_total": "assessment.estimate_total",
        "agreed_quote": "assessment.agreed_quote",
    },
    optional_inputs={"supplier_lines": "assessment.supplier_lines"},
)
def savings(
    estimate_total: Money,
    agreed_quote: Money,
    supplier_lines: dict | None = None,
) -> Money:
    """Return aggregate savings; block uncaptured per-supplier delta inputs."""

    total = Money(estimate_total - agreed_quote)
    if supplier_lines is None:
        return total
    raise CalcInputsMissing(["assessment.supplier_lines.per_supplier_delta_inputs"])


@calc(
    "C-06",
    version="1.0.0",
    inputs={
        "agreed_value": "assessment.agreed_value",
        "assessor_fee": "assessment.assessor_fee",
        "towing": "assessment.towing_fee",
    },
)
def write_off_reserve(
    agreed_value: Money, assessor_fee: Money, towing: Money
) -> Money:
    """Return the write-off reserve."""

    return Money(agreed_value + assessor_fee + towing)


@calc(
    "C-07",
    version="1.0.0",
    status="blocked_on_inputs",
    blocked_on=("formula.C-07",),
)
def pending_c07() -> None:
    """Keep the uncaptured settlement variants visible."""

    return None


@calc(
    "C-08",
    version="1.0.0",
    status="blocked_on_inputs",
    blocked_on=("formula.C-08",),
)
def pending_c08() -> None:
    """Keep the uncaptured payable calculation visible."""

    return None
