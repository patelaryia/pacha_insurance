"""Orchestration error taxonomy and the sanitised Temporal failure boundary.

Master plan section 12 fixes the closed set of failure types that may reach
Temporal history and forbids raw exception strings from crossing that boundary.
Every exception defined here names a field, a policy or a category — never the
offending value — because an error message is history and history is subject to
the AR-1 minimisation rule exactly as a payload is.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from temporalio.exceptions import ApplicationError

__all__ = [
    "NON_RETRYABLE_FAILURE_TYPES",
    "TEMPORAL_FAILURE_TYPES",
    "CodecError",
    "ConfigurationError",
    "ControlContractError",
    "HistoryPrivacyError",
    "OrchestrationError",
    "RetryPolicyError",
    "WorkflowIdError",
    "sanitised_application_error",
]


class OrchestrationError(Exception):
    """Base class for every Temporal substrate failure raised inside Pacha."""


class ConfigurationError(OrchestrationError):
    """The Temporal environment contract (section 6) is not satisfied.

    Raised before a client connects or a Worker polls, so an invalid deployment
    fails at start-up rather than part-way through an execution.
    """


class CodecError(OrchestrationError):
    """The Payload Codec refused to encode or decode (section 11).

    The Codec never falls back to plaintext, so every refusal is terminal for
    the payload that caused it.
    """


class ControlContractError(OrchestrationError):
    """A value failed the control-only payload contract (section 10)."""

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"control field {field!r} rejected: {reason}")


class WorkflowIdError(OrchestrationError):
    """A Workflow identifier was not one of the exact section 9 forms."""


class RetryPolicyError(OrchestrationError):
    """Pack retry data was malformed or widened a section 12 hard ceiling."""


class HistoryPrivacyError(OrchestrationError):
    """A privacy sentinel was found in fetched Workflow history."""


# Section 12 — the only failure types permitted in Temporal history.
TEMPORAL_FAILURE_TYPES: frozenset[str] = frozenset(
    {
        "blocked_on_inputs",
        "domain_rejected",
        "human_review_required",
        "uncertain_write",
        "ui_drift",
        "payload_diverged",
        "idempotency_conflict",
        "provider_exhausted",
        "activity_internal",
    }
)

# Section 12 — these classifications are never retried by Temporal.
NON_RETRYABLE_FAILURE_TYPES: frozenset[str] = frozenset(
    {
        "blocked_on_inputs",
        "domain_rejected",
        "human_review_required",
        "uncertain_write",
        "ui_drift",
        "payload_diverged",
        "idempotency_conflict",
    }
)


def sanitised_application_error(
    failure_type: str,
    *,
    details: Mapping[str, Any] | None = None,
) -> ApplicationError:
    """Build the only kind of `ApplicationError` an Activity may raise.

    The message is the failure type itself: an Activity records its redacted
    diagnostic detail to Pacha/Sentry and gives Temporal the classification
    only. `details` accepts control scalars — the same rules the payload
    contract applies — and is validated here so a diagnostic convenience cannot
    become a history leak.

    Args:
        failure_type: one member of `TEMPORAL_FAILURE_TYPES`.
        details: optional control-only scalars attached to the failure.

    Raises:
        ControlContractError: the type is not in the closed set, or a detail
            value is not a control scalar.
    """

    if failure_type not in TEMPORAL_FAILURE_TYPES:
        raise ControlContractError("failure_type", "not a declared Temporal failure type")

    # Imported here: `contracts` depends on nothing in this module, but keeping
    # the import local documents that the failure boundary reuses exactly the
    # payload rules rather than a second, weaker copy of them.
    from orchestration.contracts import validate_control_field

    validated: dict[str, Any] = {}
    for name, value in (details or {}).items():
        validate_control_field(name, value)
        validated[name] = value

    return ApplicationError(
        failure_type,
        validated,
        type=failure_type,
        non_retryable=failure_type in NON_RETRYABLE_FAILURE_TYPES,
    )
