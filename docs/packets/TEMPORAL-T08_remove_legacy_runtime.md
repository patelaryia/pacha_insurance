---
id: TEMPORAL-T08
prd_ref: docs/architecture/TEMPORAL_IMPLEMENTATION_MASTER_PLAN.md §19
title: Remove Celery, Beat, reaper, task wrappers and Redis
depends_on: [TEMPORAL-T07]
branch: codex/temporal-t08-remove-legacy
blast_radius: true
acceptance_tests:
  - tests/integration/test_temporal_t08.py
status: queued
pr: null
attempts: 0
reason: null
---

# TEMPORAL-T08 — Remove the legacy runtime

## 1. What to build

Delete every production item in master-plan §19, remove Celery/Redis runtime
dependencies and environment/CI services, and replace every superseded task or
Beat assertion with an equal-or-stronger Temporal Workflow/Schedule assertion.
Remove direct `AgentRunner` workflow position/recovery; retain only pure domain
helpers that Activities call.

## 2. Constraints

All T01–T07 and Graph acceptance remains green. FastAPI claim reads stay
available without Temporal. Historical docs and the isolated spike may retain
superseded terms.

## 3. Explicit non-goals

No Cloud infrastructure, business behaviour change, dual runtime or
compatibility selector.

## 4. Acceptance

The test scans production, dependencies, CI and runbooks for the complete
legacy inventory, proves all replacements are registered and runs the full
SQLite/PostgreSQL/frontend suite.
