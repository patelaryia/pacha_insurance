---
id: TEMPORAL-T05
prd_ref: docs/architecture/TEMPORAL_IMPLEMENTATION_MASTER_PLAN.md §18 T05;
  docs/PRD-05_Intake_and_Triage_Agent_v1.1.md; docs/PRD-07_Assessment_Orchestration_Agent_v1.1.md
title: Intake and assessment Temporal Workflows
depends_on: [TEMPORAL-T04]
branch: codex/temporal-t05-intake-assessment
blast_radius: false
acceptance_tests:
  - tests/acceptance/test_packet_14_intake_flow.py
  - tests/acceptance/test_packet_16_assessment_dispatch.py
  - tests/acceptance/test_packet_17_assessment_cascade.py
  - tests/integration/test_temporal_t05.py
status: queued
pr: null
attempts: 0
reason: null
---

# TEMPORAL-T05 — Intake and assessment Workflows

## 1. What to build

Add pinned `IntakeWorkflow` and `AssessmentWorkflow` definitions. Intake uses
`pacha.intake.{intake.requested event ULID}` and invokes the existing eight COP
steps as the exact named Activities `intake_create_claim`, `intake_ingest`,
`intake_populate`, `intake_dupe_check`, `intake_late_check`,
`intake_acknowledge`, `intake_checklist`, and `intake_triage`.

An Activity returning `waiting` or `awaiting_review` leaves the Workflow on an
opaque Signal wait. `intake.document_ready`, `intake.review_resolved`, and
`intake.claim_terminal` are committed control events carrying run/claim/source
references only. The retried Activity reloads the committed event and current
PostgreSQL state. Claim creation remains one idempotent PostgreSQL transaction;
the claim ULID is projected into `agent_runs` without changing Workflow ID.

Assessment uses `pacha.assessment.{agent_run ULID}` and the exact Activity
surface `assessment_prepare`, `assessment_mode_shadow`,
`assessment_apply_mode_review`, `assessment_dispatch`,
`assessment_parse_report`, `assessment_cascade`, and
`assessment_record_terminal`. The mode card and permanent-L0 shadow attempt
remain independent: shadow failure never suppresses the governed card.
`assessment.review_resolved`, `assessment.report_ready`, and
`assessment.claim_terminal` carry opaque references only. Dispatch executes
through `execute_or_stage` on the effects queue with one Temporal attempt.

## 2. Constraints

Consume all PACKET-14/16/17 domain services, review schemas, autonomy ceilings,
vendor registry, append-only writes and cascade semantics unchanged. Temporal
owns position and recovery only. Every Activity reloads claim facts internally;
history contains no message body, recipient, registration, estimate or money.

## 3. Explicit non-goals

Do not migrate approval packs, projection, Graph transport or schedules. Do
not retain a second production `AgentRunner.start/run` path. Missing Graph send
transport remains a visible governed refusal until PACKET-24.

## 4. Acceptance

The named suites prove stable IDs and duplicate attachment, exact step order,
human Signal waits, one-attempt dispatch, restart/replay, control-only history,
terminal suppression and claim reads with Temporal stopped.
