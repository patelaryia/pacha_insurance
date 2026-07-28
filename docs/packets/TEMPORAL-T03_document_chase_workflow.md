---
id: TEMPORAL-T03
prd_ref: docs/architecture/TEMPORAL_IMPLEMENTATION_MASTER_PLAN.md §2 (T03),
  docs/PRD-06_Document_Chase_Agent_v1.1.md, Section 0.5 AR-1/AR-2
title: Complete PRD-06 document-chase Workflow
depends_on:
- TEMPORAL-T02
status: merged
branch: codex/temporal-t03-document-chase
attempts: 0
blast_radius: true
acceptance_tests:
- tests/integration/test_temporal_t03.py
review_findings: []
pr: https://github.com/patelaryia/pacha_insurance/pull/29
reason: null
---

# TEMPORAL-T03 — Document-chase Workflow

**Historical board record.** PR
[#29](https://github.com/patelaryia/pacha_insurance/pull/29) merged on
2026-07-28. This file makes the completed packet reconstructible; it is not a
re-issue of the spec and authorises no additional T03 product work.

The executable spec is
`docs/architecture/TEMPORAL_IMPLEMENTATION_MASTER_PLAN.md` §2, row T03:
"Complete PRD-06 document-chase Workflow", excluding all other agents.

## Acceptance

`tests/integration/test_temporal_t03.py` is T03's acceptance suite. It runs the
production `DocumentChaseWorkflow`, Activities, intent bridge and Worker
registration against a real time-skipping Temporal server. It covers initial
request, durable timers, inbound deferral, document and review Signals,
terminal suppression, replay and history privacy.

The suite is pinned by content hash in `loop/oracle.lock`.

## Constraints retained

T03 retains AR-1 control-only Workflow history, AR-2 governed sends, stable
checklist-derived Workflow/write identities, stage-row and event authority in
PostgreSQL, and no polling `ChaseAgent.tick()` production path.
