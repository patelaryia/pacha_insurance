"""Closed PRD-02 verb and PRD-04 review-item contracts consumed by COP."""

OUTCOME_ACTIONS = frozenset(
    {
        "set_field",
        "route_review",
        "propose_decline",
        "block",
        "emit_event",
        "route_approval",
    }
)

REVIEW_ITEM_TYPES = frozenset(
    {
        "FIELD_VERIFY",
        "DOC_CLASSIFY",
        "DOC_SPLIT",
        "CONSISTENCY_FLAG",
        "DRAFT_RELEASE",
        "MODE_CONFIRM",
        "NOTE_REVIEW",
        "PACK_REVIEW",
        "EX_GRATIA",
        "EXCEPTION",
        "PROMOTION_SIGNOFF",
        "SAMPLE_REVIEW",
        "PASTE_READBACK_CHECK",
        "PROCEED_PARTIAL",
        "KYC_VERIFY",
        "EFT_MATCH",
        "REOPEN_PROMPT",
    }
)

__all__ = ["OUTCOME_ACTIONS", "REVIEW_ITEM_TYPES"]
