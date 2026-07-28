---
description: Weekly architecture drift report over everything the loop merged since the last run. Report only, never opens a PR.
argument-hint: (none — reads loop/drift/LAST_RUN)
allowed-tools: Read, Glob, Grep, Write, Bash(git *), Bash(python3 loop/controller.py *)
---

# /drift — what have the merged packets become together?

The per-packet loop never looks at this. A packet boundary is a **review**
boundary, not an **architecture** boundary: every packet can be individually
correct and the system they compose can still be incoherent. Nothing else in
this design would notice.

**You report. You do not open a PR, you do not edit code, you do not create
packets.** The output is one file the owner reads. If a finding deserves
work, the owner slices it.

## Procedure

**1. Establish the window.**

```bash
cat loop/drift/LAST_RUN            # a commit sha, or empty on first run
git log --oneline <sha>..HEAD
git diff --stat <sha>..HEAD
```

Empty file → use the last 30 days.

**2. Read the accumulated diff**, not the individual packets. You are
looking for what emerged from the combination, which by definition is
invisible packet by packet.

**3. Look for exactly these four things.** Do not broaden the brief — a
drift report that also reviews code quality gets skimmed, and the four
things below are the ones nothing else catches.

**Duplicated abstractions.** Two modules that now solve the same problem
differently. Two idempotency-key helpers. Two ways to write the ledger. Two
ULID generators. Grep for the repo's settled patterns and count the
implementations: `execute_or_stage`, append-only field writes, the
single-writer ledger, StrictUndefined rendering, error shapes, retry
policies, `Money` construction.

**Contradictory patterns across packets.** Packet A validates at the
handler; packet B validates in the service. Packet C returns 409 for a stale
write; packet D returns 422. Packet E puts thresholds in the pack; packet F
hard-codes them. Each was defensible under its own spec. Together they mean
a reader must know which packet wrote a file to know which convention
applies.

**Invariants that eroded.** Take CLAUDE.md §2's ten invariants and check
each against the current tree rather than against a diff. Erosion is
gradual and per-packet-invisible by construction: a new adapter call
outside the gate, an in-place `claim_fields` update in a code path added
later, a review-item type that made 18, an autonomy ceiling widened by a
config change nobody read as a ceiling change, a `float` reaching a money
path through a JSON round-trip that the ED-8 lint cannot see.

**Dead code the loop left behind.** Rework cycles leave orphans: the first
attempt's helper that the third attempt stopped calling, a config key
nothing reads, a migration for a column no model references, a fixture for
a deleted scenario, an abstraction introduced for a second caller that never
arrived (code-standards §2.1).

**4. Write `loop/drift/YYYY-MM-DD.md`** with, per finding: what it is, the
files, which packets introduced each side, why it is a problem now rather
than in the abstract, and the smallest thing that would fix it. Findings
only — no ranking theatre, no "overall the architecture is healthy".

If you find nothing, say so in one line. A short honest report is the
correct output for a good week and makes the bad weeks legible.

**5. Record the window.**

```bash
git rev-parse HEAD > loop/drift/LAST_RUN
```

## What not to report

- Anything a per-packet review already catches. That is not drift.
- Style, naming, formatting.
- Anything you would fix by opening a PR — you are not opening a PR, so a
  finding you cannot state as an architectural observation is out of scope.
