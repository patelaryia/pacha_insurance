---
id: TEMPORAL-T01
prd_ref: docs/architecture/TEMPORAL_IMPLEMENTATION_MASTER_PLAN.md §2 (T00/T01), ADR-001,
  Section 0 ED-2, Section 0.5 AR-1/AR-1a
title: Shared Temporal package, configuration, Codec, Worker bootstrap and SDK tests
depends_on:
- PACKET-20
status: merged
branch: claude/temporal-substrate-t01-fa9ffb
attempts: 0
blast_radius: true
acceptance_tests:
- tests/integration/test_temporal_orchestration.py
review_findings: []
pr: https://github.com/patelaryia/pacha_insurance/pull/26
merged_at: '2026-07-27T06:26:51Z'
---

# TEMPORAL-T01 — Shared Temporal substrate

**Board record.** This packet predates the loop and was built and merged by
hand. The file exists so the dependency graph is closed: TEMPORAL-T02
declares `depends_on: [TEMPORAL-T01]`, and `loop/board.py` refuses to load a
board whose `depends_on` names a packet that is not on it.

The executable spec is
`docs/architecture/TEMPORAL_IMPLEMENTATION_MASTER_PLAN.md` §2, row T01:
"Shared Temporal package, configuration, Codec, Worker bootstrap and SDK
tests", explicitly excluding agent migration and Celery deletion.

Landed as `97b8831` (`feat(orchestration): add shared Temporal substrate`)
on top of the T00 architecture freeze `920b9d3`.

## Non-goals

Everything the master plan assigns to T02 and later: the `agent_runs`
projection, the outbox bridge, system drain workflows, review signal
routing, and any business-agent migration.
