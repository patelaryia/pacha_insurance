"""Centralised Workflow identity builders (master plan section 9).

Every Workflow ID Pacha ever uses is constructed here, from a Pacha business
ULID that was generated and committed *before* the start attempt. That ordering
is what makes `WorkflowIDConflictPolicy.USE_EXISTING` safe: a retried start
reuses the same ID and attaches to the existing execution instead of duplicating
domain work.

A builder therefore never mints an identifier. It takes one, proves it is a
ULID, and returns the single legal string form. An ID derived from a timestamp,
a name, a policy number, a registration plate or a hash of PII is rejected by
construction, because none of those is a 26-character uppercase ULID.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from orchestration.contracts import WORKFLOW_ID_KINDS, WORKFLOW_ID_PATTERN, validate_control_field
from orchestration.errors import ControlContractError, WorkflowIdError

__all__ = [
    "WORKFLOW_ID_KINDS",
    "WorkflowRef",
    "agent_workflow_ref",
    "approval_pack_workflow_ref",
    "assessment_workflow_ref",
    "chase_workflow_ref",
    "docintel_workflow_ref",
    "intake_workflow_ref",
    "parse_workflow_ref",
    "projection_workflow_ref",
]

_ULID_PATTERN = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")


@dataclass(frozen=True, slots=True)
class WorkflowRef:
    """One Temporal Workflow identity, in the exact section 9 string form.

    The single field is the Workflow ID itself, so a `WorkflowRef` is also a
    valid `workflow_ref` control value and may be carried in a payload without
    any further unpacking.
    """

    workflow_ref: str

    def __post_init__(self) -> None:
        validate_control_field("workflow_ref", self.workflow_ref)

    @property
    def kind(self) -> str:
        """The section 9 Workflow family, e.g. `chase` or `approval-pack`."""

        return parse_workflow_ref(self.workflow_ref)[0]

    @property
    def subject_ref(self) -> str:
        """The Pacha business ULID the identity was derived from."""

        return parse_workflow_ref(self.workflow_ref)[1]

    def __str__(self) -> str:
        return self.workflow_ref


def _require_ulid(kind: str, ulid: str) -> str:
    if not isinstance(ulid, str) or not _ULID_PATTERN.fullmatch(ulid):
        raise WorkflowIdError(
            f"{kind} Workflow ID requires an uppercase 26-character Pacha ULID"
        )
    return ulid


def _build(kind: str, ulid: str) -> WorkflowRef:
    return WorkflowRef(f"pacha.{kind}.{_require_ulid(kind, ulid)}")


def agent_workflow_ref(agent_run_ref: str) -> WorkflowRef:
    """`pacha.agent.{agent_run_ulid}` — the generic agent run."""

    return _build("agent", agent_run_ref)


def chase_workflow_ref(checklist_ref: str) -> WorkflowRef:
    """`pacha.chase.{checklist_ulid}` — one execution per PRD-06 checklist."""

    return _build("chase", checklist_ref)


def docintel_workflow_ref(document_ref: str) -> WorkflowRef:
    """`pacha.docintel.{document_ulid}` — one execution per document."""

    return _build("docintel", document_ref)


def intake_workflow_ref(trigger_event_ref: str) -> WorkflowRef:
    """`pacha.intake.{trigger_event_ulid}`.

    Keyed on the trigger event so a duplicate mail or webhook delivery attaches
    to the existing execution rather than creating a second claim.
    """

    return _build("intake", trigger_event_ref)


def assessment_workflow_ref(agent_run_ref: str) -> WorkflowRef:
    """`pacha.assessment.{agent_run_ulid}`."""

    return _build("assessment", agent_run_ref)


def approval_pack_workflow_ref(agent_run_ref: str) -> WorkflowRef:
    """`pacha.approval-pack.{agent_run_ulid}`."""

    return _build("approval-pack", agent_run_ref)


def projection_workflow_ref(projection_ref: str) -> WorkflowRef:
    """`pacha.projection.{projection_ulid}`."""

    return _build("projection", projection_ref)


def parse_workflow_ref(workflow_ref: str) -> tuple[str, str]:
    """Split a Workflow ID into `(kind, ulid)`, refusing any other string."""

    try:
        validate_control_field("workflow_ref", workflow_ref)
    except ControlContractError as error:
        raise WorkflowIdError("not a declared Workflow-ID form") from error
    match = WORKFLOW_ID_PATTERN.fullmatch(workflow_ref)
    assert match is not None  # noqa: S101 - guaranteed by the validation above
    kind = match.group(1)
    return kind, workflow_ref[len("pacha.") + len(kind) + 1 :]
