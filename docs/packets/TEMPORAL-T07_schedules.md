---
id: TEMPORAL-T07
prd_ref: docs/architecture/TEMPORAL_IMPLEMENTATION_MASTER_PLAN.md §16
title: Temporal Schedules for all recurring platform jobs
depends_on: [PACKET-24]
branch: codex/temporal-t07-schedules
blast_radius: false
acceptance_tests:
  - tests/unit/test_notify_packet12.py
  - tests/acceptance/test_packet_09_eval_corpus.py
  - tests/acceptance/test_packet_20_projection_paste_assist.py
  - tests/integration/test_temporal_t07.py
status: queued
pr: null
attempts: 0
reason: null
---

# TEMPORAL-T07 — Recurring Schedules

## 1. What to build

Add `orchestration.schedules.bootstrap_schedules` and the five finite wrappers
named in master-plan §16. Create all nine exact Schedule IDs, timings, overlap
policies and catch-up windows. The Graph wrappers call PACKET-23/24 services;
missing registration refuses bootstrap with `GRAPH_SERVICE_NOT_INSTALLED`.

Bootstrap creates missing schedules, compares every existing definition and
fails on drift. It never updates or deletes an existing schedule. Each wrapper
invokes one idempotent Activity and terminates.

## 2. Constraints

Cadence comes from the master plan or existing pack data. No cron Workflow,
Celery Beat, business logic in Workflow code or successful no-op.

## 3. Explicit non-goals

No legacy deletion or infrastructure. T08 and T09 own those boundaries.

## 4. Acceptance

The tests exercise exact definitions, idempotent bootstrap, mismatch refusal,
real time-skipping executions, control-only history and Graph prerequisite
refusal.
