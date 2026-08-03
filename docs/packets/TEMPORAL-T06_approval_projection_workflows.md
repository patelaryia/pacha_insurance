---
id: TEMPORAL-T06
prd_ref: docs/architecture/TEMPORAL_IMPLEMENTATION_MASTER_PLAN.md §18 T06;
  docs/PRD-08_Approval_Pack_Generator_v1.1.md; docs/PRD-09_System_Projection_and_Reconciliation_v1.1.md
title: Approval-pack and projection Temporal Workflows
depends_on: [TEMPORAL-T05]
branch: codex/temporal-t06-approval-projection
blast_radius: true
acceptance_tests:
  - tests/acceptance/test_packet_18_approval_pack_backend.py
  - tests/acceptance/test_packet_19_approval_workflow.py
  - tests/acceptance/test_packet_20_projection_paste_assist.py
  - tests/integration/test_temporal_t06.py
status: queued
pr: null
attempts: 0
reason: null
---

# TEMPORAL-T06 — Approval-pack and projection Workflows

## 1. What to build

Add pinned `ApprovalPackWorkflow` with ID
`pacha.approval-pack.{agent_run ULID}` and Activities
`approval_resolve_manifest`, `approval_merge`, `approval_generate_note`,
`approval_grade_and_queue`, `approval_apply_review`,
`approval_prepare_signature`, `approval_finalize_signature`, and
`approval_record_terminal`. PACK_REVIEW, NOTE_REVIEW and authority decisions
resume only from committed `approval.review_resolved` events. PDF/HTML bytes
remain in the blob store.

Add pinned `ProjectionWorkflow` with ID
`pacha.projection.{projection ULID}` and Activities
`projection_prepare`, `projection_execute_or_stage`, `projection_readback`,
`projection_reconcile`, and `projection_record_terminal`. Paste-assist performs
no external effect. Any later executor runs on the effects queue with one
attempt and a stable write ID. Readback is always separate.
`uncertain_write`, `ui_drift` and `payload_diverged` finish visibly blocked
and are never retried or repaired.

The initiating transactions prepare `agent_runs`/projection rows and emit
`approval.workflow_requested` or `projection.workflow_requested` with opaque
identifiers before the T02 bridge starts either Workflow.

## 2. Constraints

Preserve StrictUndefined, verification floors, immutable artifacts, signing
authority, GP-1, the operation catalogue, paste sampling and every existing
idempotency/readback invariant. Temporal completion is not external-write truth.

## 3. Explicit non-goals

No Celery deletion, Graph implementation, Schedule creation, RPA vendor,
auction provider or payment execution. A vendor-mode request stays
`blocked_on_inputs` until its separately approved provider contract exists.

## 4. Acceptance

The named suites prove exact Activity surfaces, real Signal waits, replay,
history privacy, crash-safe signing, paste-assist completion, one-attempt
effects and blocked uncertain/diverged outcomes.
