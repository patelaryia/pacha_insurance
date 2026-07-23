"""The synthetic 14-field `edms.claims_workflow` definition (tests only).

Deliberately *not* the production path. No EDMS selector, dropdown map, failure
signature, or folder-reference format has been captured; these values exist so
the executable contract, the reconciliation mapping, the prior-completion probe,
and the two known-failure handlers can be proved against a deterministic target
served inside the test process.
"""

from __future__ import annotations

from typing import Any

FOLDER_REF = "EDMS/2026/004521"
FOLDER_REF_PATTERN = r"^EDMS/[0-9]{4}/[0-9]{6}$"

#: The exact fourteen payload-backed fields, in target order.
EDMS_FIELD_PATHS: tuple[tuple[str, str, str, str], ...] = (
    # (step id, field path, external encoding, target selector)
    ("s1", "policy.number", "raw", "#policyNo"),
    ("s2", "parties.insured.name", "raw", "#insuredName"),
    ("s3", "parties.insured.phone", "raw", "#insuredPhone"),
    ("s4", "parties.insured.national_id", "raw", "#insuredId"),
    ("s5", "parties.insured.kra_pin", "raw", "#insuredPin"),
    ("s6", "parties.insured.dl_number", "raw", "#insuredDl"),
    ("s7", "parties.insured.bank_account", "raw", "#insuredBank"),
    ("s8", "loss.description", "raw", "#lossDescription"),
    ("s9", "loss.date", "iso", "#lossDate"),
    ("s10", "intimation.received_at", "iso", "#intimatedAt"),
    ("s11", "intimation.channel", "raw", "#intimationChannel"),
    ("s12", "policy.excess", "cents", "#excessCents"),
    ("s13", "reserve.total", "shillings", "#reserveShillings"),
    ("s14", "settlement.amount", "cents", "#settlementCents"),
)
NORMALISERS = {
    "raw": {"string": "string_exact", "enum": "enum_exact"},
    "iso": {"date": "date_iso_exact", "datetime": "datetime_iso_exact"},
    "cents": {"money": "money_cents_exact"},
    "shillings": {"money": "money_shillings_to_cents_exact"},
}
VALUE_TYPES = {
    "policy.number": "string",
    "parties.insured.name": "string",
    "parties.insured.phone": "string",
    "parties.insured.national_id": "string",
    "parties.insured.kra_pin": "string",
    "parties.insured.dl_number": "string",
    "parties.insured.bank_account": "string",
    "loss.description": "string",
    "loss.date": "date",
    "intimation.received_at": "datetime",
    "intimation.channel": "enum",
    "policy.excess": "money",
    "reserve.total": "money",
    "settlement.amount": "money",
}
#: Durable claim values the fixture seeds before requesting a projection.
SEED_VALUES: dict[str, tuple[Any, str]] = {
    "policy.number": ("POL-21-0001", "string"),
    "parties.insured.name": ("Grace Wanjiru", "string"),
    "parties.insured.phone": ("+254700000021", "string"),
    "parties.insured.national_id": ("21000021", "string"),
    "parties.insured.kra_pin": ("A002100021X", "string"),
    "parties.insured.dl_number": ("DL2100021", "string"),
    "parties.insured.bank_account": ("0102100021", "string"),
    "loss.description": ("Rear-end collision on Mombasa Road", "string"),
    "loss.date": ("2026-07-01", "date"),
    "intimation.received_at": ("2026-07-02T06:30:00+00:00", "datetime"),
    "intimation.channel": ("email", "enum"),
    "policy.excess": (2_500_00, "money"),
    "reserve.total": (14_265_600, "money"),
    "settlement.amount": (9_000_00, "money"),
}


def _step(step_id: str, path: str, encoding: str, selector: str, order: int) -> dict[str, Any]:
    return {
        "id": step_id,
        "screen": "details" if order <= 8 else "workflow",
        "action": "select" if path == "intimation.channel" else "fill",
        "selector": selector,
        "value": "{" + path + "}",
        "value_kind": "field",
        "external_encoding": encoding,
        "effect": "local_input",
        "timeout_class": "edms",
        "write_id": None,
        "postcondition": {"kind": "exact_value", "selector": selector},
        "paste_assist": {"label": path, "copy": True},
    }


EDMS_CLAIMS_WORKFLOW: dict[str, Any] = {
    "operation": "edms.claims_workflow",
    "version": "1.0.0",
    "status": "live",
    "preconditions": [
        {"assert": "logged_in"},
        {"assert": "module", "equals": "Claims Workflow"},
    ],
    "screens": [
        {"id": "details", "label": "Claim details", "order": 1},
        {"id": "workflow", "label": "Workflow", "order": 2},
    ],
    "steps": [
        *(
            _step(step_id, path, encoding, selector, order)
            for order, (step_id, path, encoding, selector) in enumerate(EDMS_FIELD_PATHS, 1)
        ),
        {
            "id": "s15",
            "screen": "workflow",
            "action": "click",
            "selector": "role=button[name='Submit']",
            "effect": "external_write",
            "timeout_class": "edms",
            "write_id": "submit_workflow",
            "postcondition": {"kind": "visible", "selector": "#workflowReference"},
        },
    ],
    "readback": [
        {
            "capture": "folder_ref",
            "label": "EDMS folder reference",
            "into": "external.edms.folder_ref",
            "assert_format": "edms_folder_ref_regex",
            "required": True,
            "selector": "#workflowReference",
        }
    ],
    "validators": {
        "edms_folder_ref_regex": {"status": "live", "pattern": FOLDER_REF_PATTERN}
    },
    "reconciliation": [
        {
            "step_id": step_id,
            "selector": selector,
            "normaliser": {"kind": NORMALISERS[encoding][VALUE_TYPES[path]]},
        }
        for step_id, path, encoding, selector in EDMS_FIELD_PATHS
    ],
    "retry_probe": {
        "keys": [{"from_step": "s1", "target": "policy_number"}],
        "exact_match": "complete_without_write",
        "absent": "retry_only_if_no_external_write_completed",
        "ambiguous": "uncertain_write",
    },
    "known_failures": {
        "duplicate_filename": {
            "signature": "EDMS-DUP-FILENAME",
            "handler": "duplicate_filename",
        },
        "slow_reflection": {
            "signature": "EDMS-NOT-REFLECTED",
            "handler": "slow_reflection",
        },
    },
    "failure_policy": "screenshot_always, halt_on_selector_miss, no_guessing",
}

EDMS_LIVE_ROW: dict[str, Any] = {
    "id": "edms.claims_workflow",
    "version": "1.0.0",
    "system": "edms",
    "mode": "rpa",
    "status": "live",
    "blocked_on": None,
    "click_path_ref": "edms.claims_workflow@1.0.0.yaml",
    "owner_prd": "PRD-09",
}


__all__ = [
    "EDMS_CLAIMS_WORKFLOW",
    "EDMS_FIELD_PATHS",
    "EDMS_LIVE_ROW",
    "FOLDER_REF",
    "FOLDER_REF_PATTERN",
    "SEED_VALUES",
    "VALUE_TYPES",
]
