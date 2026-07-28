---
id: TEMPORAL-T03
prd_ref: docs/architecture/TEMPORAL_IMPLEMENTATION_MASTER_PLAN.md §2 (T03),
  docs/PRD-06_Document_Chase_Agent_v1.1.md, Section 0.5 AR-1/AR-2
title: Complete PRD-06 document-chase Workflow
depends_on:
- TEMPORAL-T02
status: escalated
branch: codex/temporal-t03-document-chase
attempts: 0
blast_radius: true
acceptance_tests: []
review_findings: []
pr: https://github.com/patelaryia/pacha_insurance/pull/29
escalation_reason: 'No acceptance test names this packet''s definition of done. PR
  #29 is open and building against the master plan §2 T03 row, but the branch adds
  no test file of its own: `git ls-tree origin/codex/temporal-t03-document-chase --
  tests/` returns only the PACKET-01..20 suites. Under the loop''s rules this packet
  is not dispatchable and not reviewable — there is nothing to be green against. Needs
  the owner to either point it at an existing suite or have /slice write the failing
  tests first.'
---

# TEMPORAL-T03 — Document-chase Workflow

**Board record for work already in flight.** PR
[#29](https://github.com/patelaryia/pacha_insurance/pull/29) was opened
before the loop existed. This file puts it on the board so its state is
reconstructible; it is not a re-issue of the spec.

The executable spec is
`docs/architecture/TEMPORAL_IMPLEMENTATION_MASTER_PLAN.md` §2, row T03:
"Complete PRD-06 document-chase Workflow", excluding all other agents.

## Why this is escalated rather than at review

The loop's stopping condition is a packet's named acceptance tests. This
packet names none, and none exist on its branch:

```
git ls-tree -r --name-only origin/codex/temporal-t03-document-chase -- tests/
# → tests/acceptance/test_packet_01..20 and tests/ci/* only
```

`tests/acceptance/test_packet_15_chase_agent.py` covers the PRD-06 chase
agent as it existed before the Temporal migration. It is not a T03
acceptance suite: nothing in it asserts the Workflow's durability
properties, which is the entire content of the T03 row.

`loop/board.py` therefore refuses to hold this packet at `status: review` —
a packet at review with no acceptance test has no definition of done, and
approving it would mean the reviewer inventing one. That is the failure mode
the whole design exists to prevent, so the board fails closed instead.

## What unblocks it

One of:

1. the owner names an existing suite that genuinely pins T03's contract, and
   sets `acceptance_tests` accordingly; or
2. `/slice docs/architecture/TEMPORAL_IMPLEMENTATION_MASTER_PLAN.md` cuts
   T03 properly and writes the failing tests first, as a separate
   CODEOWNERS-reviewed commit, after which the existing PR is judged against
   them.

Do not resolve this by writing a test that describes what PR #29 already
does. That is the definition of done being written by the thing it is
supposed to constrain.
