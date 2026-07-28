---
id: TEMPORAL-T04
prd_ref: docs/architecture/TEMPORAL_IMPLEMENTATION_MASTER_PLAN.md §2 (T04),
  §4, §7, §9, §12, §18 and §22; docs/PRD-01_Document_Intelligence_Engine_v1.1.md
  §1.2–§1.7; Section 0.5 AR-1
title: PRD-01 document-intelligence Workflow and long-running Activities
depends_on:
- TEMPORAL-T03
status: queued
branch: codex/temporal-t04-document-intelligence
attempts: 0
blast_radius: true
acceptance_tests:
- tests/acceptance/test_packet_04_docintel_substrate.py
- tests/acceptance/test_packet_05_docintel_live_model.py
- tests/integration/test_temporal_t04.py
review_findings: []
pr: null
reason: null
---

# TEMPORAL-T04 — Document-intelligence Workflow

## 1. What to build

Replace PRD-01's Celery stage scheduler with one finite, pinned
`DocumentIntelligenceWorkflow` per document ULID. Its Workflow ID is exactly
`pacha.docintel.{document_ulid}` and duplicate `document.received` delivery
attaches to that execution through the T02 transactional-outbox bridge.

The Workflow invokes these eight explicit Activity types in the binding PRD-01
order:

1. `docintel_normalize`
2. `docintel_classify`
3. `docintel_split`
4. `docintel_extract`
5. `docintel_cite`
6. `docintel_validate`
7. `docintel_commit`
8. `docintel_consistency`

Every Workflow/Activity argument contains only the document ULID and the
stage/checkpoint control identifier. Activities load documents, object bytes,
stage rows, model configuration and claim data inside Pacha. OCR, LLM and
vision work runs on `pacha-{env}-docintel-v1`, heartbeats every 30 seconds with
stage/checkpoint integers only, and never returns document content or extracted
facts to Workflow history.

`document_stages` remains the idempotency and restart authority. A terminal
stage is observed, not repeated. A paused or failed stage does not advance and
can resume only after an authorised opaque Signal backed by a committed Pacha
event. Provider-wrapper Activities use one Temporal attempt so Temporal cannot
multiply ED-4a retries.

Register the production Workflow and Activities explicitly with the existing
Worker factory. Remove `CeleryStageScheduler`, the doc-intel Celery task
wrappers and queue-routing configuration used only by that scheduler. Preserve
the root Celery dependency and other agents' Celery paths for T08.

## 2. Constraints consumed unchanged

- PRD-01's exact eight-stage order, stage-row statuses, document schemas,
  confidence rules, citation algorithm and append-only commit behaviour remain
  unchanged.
- No field reaches `extracted` without resolved provenance.
- Temporal history contains no document bytes, names, policy/registration
  values, money, bank/customer data, model prompts/outputs or raw exceptions.
- Claim reads and committed document state remain available when Temporal is
  stopped.
- The T01 Codec, control contracts, retry ceilings and pinned Worker deployment
  rules, plus the T02 outbox authority, are reused rather than wrapped.

## 3. Explicit non-goals

T04 does not migrate intake or assessment (T05), approval or projection (T06),
create recurring Schedules (T07), or remove Celery/Redis globally (T08). It
does not change PRD-01 extraction behaviour, schemas, thresholds, citations,
review-item semantics, model budgets or SLO values.

An attempt to route an intake/assessment event through
`DocumentIntelligenceWorkflow` has no registered mapping and starts nothing.
A stage without a committed resume event remains visibly paused/failed; no
timer or retry guesses that it is safe to continue.

## 4. Acceptance

The PACKET-04/05 suites preserve the complete PRD-01 substrate, live-stage,
failure and provenance behaviour. `tests/integration/test_temporal_t04.py`
pins the Temporal-specific definition of done: exact stable routing, pinned
Workflow and Activity surface, real time-skipping execution in exact stage
order on the docintel queue, control-only history, and removal of the
doc-intel Celery scheduler without deleting the later T08 dependency.

The packet is one integrity boundary: the old stage scheduler is removed only
in the same PR that proves the complete Temporal replacement.
