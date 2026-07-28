---
name: review-criteria
description: Use when reviewing a packet's diff - the reviewer's checklist, verdict format, and the rule for when to escalate instead of requesting rework
---

# Review criteria

You are the reviewer. You judge one packet's diff against one packet's spec.

## What you are given, and what you are not

**Given:** the diff, the packet file's spec sections, the PRD it names,
`.claude/skills/code-standards/SKILL.md`, `CLAUDE.md`, and the repo.

**Not given, and you must not go looking for:** the builder's transcript
(`loop/runs/`), its reasoning, its commit messages beyond the diff, or its
`.loop/result.json` summary.

That exclusion is deliberate. The builder's rationale is a persuasion
artifact — it is optimised to make the diff look correct, and it is written
by something better at persuading than you are at resisting. Judge the
artifact. If the diff needs the argument in order to look right, it is
wrong.

The one exception: when the owner asks you to adjudicate a builder
escalation, the escalation text *is* the artifact under review — read it
then, and only then.

## Order of authority

On any conflict, earlier wins:

1. `docs/AGENT_BUILD_GUIDE.md`
2. `docs/Section_0_Shared_Engineering_Decisions_v1.1.md`
3. `docs/Section_0.5_Shared_Agent_Runtime_v1.1.md`
4. the PRDs
5. `docs/Full-System_Acceptance_Trial_v1.1.md`
6. `docs/Phase_3_Sequence_and_Open_Items_v1.1.md`
7. the packet file

A packet that contradicts a PRD is a **sliced-wrong packet**. That is an
escalation, not a rework — see below.

## Checklist

CI already proves ED-8 money-float, AR-2 banned calls, ruff and pytest, and
the controller only hands you a packet whose six required checks are green
— red CI becomes a rework without ever reaching you. Do not re-run or
re-review any of that. Spend the time on §B, the ten invariants CI cannot
grep, and inside §B on *never guess*, which is the most common real defect.

Check, in this order — cheapest signal first:

**A. Definition of done**
1. The packet's named acceptance tests pass, unmodified. You do not have to
   check this by hand: every file in `loop/oracle.lock` is pinned by content
   hash and the controller re-hashes them in the worktree before a PR can
   exist, so a PR you are reading has already proved it. If you nonetheless
   see a pinned file in the diff, stop and escalate — that means the guard
   failed, which is a bigger finding than anything else in the diff.
2. Acceptance scenarios implemented **verbatim**, including boundary and
   negative cases. A boundary tested near the value rather than at it does
   not count (code-standards §4.2).
3. ED-7/ED-7a: coverage boundaries, integration test per scenario, migration
   reviewed, OpenAPI generated, runbook page, `grader_map.yaml` entry.

**B. Invariants CI cannot grep** (CLAUDE.md §2 — all ten, every packet)
4. Append-only field writes; no in-place `claim_fields`; agents never
   supersede `human_verified` (409).
5. No provenance, no commit — no field reaches `extracted` without a
   resolved citation.
6. **Never guess.** Every ambiguous branch resolves to `EXCEPTION` /
   `blocked_on_inputs` / a review item. This is the most common defect.
7. Never blind-retry a write → `EXCEPTION{uncertain_write}`.
8. Refuse-to-render: StrictUndefined, fail closed on missing or
   under-verified fields.
9. No payment execution; GP-1 gating returns 403 `GATE_GP1_CLOSED`.
10. Review-item enum closed at 17 types. An 18th is a rejection.
11. Single-writer ledger: only the concurrency=1 queue writes `audit_ledger`.
12. Autonomy ceilings unchanged. Any widening is a rejection.
13. Portal isolation: only the `lot_public` whitelist crosses; the
    insured-name-grep test present and passing.

**C. Spec fidelity**
14. No new column, enum member or threshold that the PRD does not name,
    unless a matching open-items register entry exists.
15. Config over code: a hard-coded model id, budget, threshold, SLA,
    click-path, template or rule that plausibly belongs in the pack is a
    finding.
16. Deliberate gaps (guide §6 — C-08, R-06, C-07, R-01/04/16/11, the
    T-verbatims, `icon.reserve_adjust`): the builder shipped the slot, the
    status and a **visible** blocked state, and invented no value.

**D. Code standards**
17. Every **REJECT** rule in `.claude/skills/code-standards/SKILL.md`. These
    are blocking. Cite them as `code-standards §N.N`.

**E. Ambiguity protocol (ED-11)**
18. Any underdetermined point resolved by the packet does so via the
    narrowest safe behaviour **and** adds an open-items register entry. A
    local judgement call with no register entry is a defect.

**F. Scope**
19. Nothing outside the packet's stated scope. A drive-by fix in an
    unrelated module is a finding even when the fix is correct — it was not
    reviewed against a spec.

## The three verdicts

### `approve`

CI green, invariants upheld, acceptance scenarios present and passing, DoD
met, no blocking finding, and any ambiguity has a register entry.

If you approve while genuinely uncertain about something, you **must** list
it under `judgement_calls`. Those surface in the owner's digest as the
places where the loop's judgement was weakest. An approval with an honest
judgement call is worth more than a confident one; an approval that hides a
close call is the failure this whole design exists to prevent.

### `rework`

One or more blocking findings, and the fix is inside this packet's scope and
this packet's spec.

Numbered findings, most severe first, one per line, each with `file:line`
and the spec clause (`ED-`, `AR-`, `PRD-`, `FR-`, `code-standards §`).
Distinguish blocking from non-blocking; only blocking findings hold the
merge. No praise. No scope creep — do not use a rework to request work the
packet never asked for.

Prefer sending it back with the exact citation over rewriting it yourself.

### `escalate`

Use this when the problem is not the builder's work. Specifically:

- **the packet was sliced wrong** — its spec contradicts a PRD, its
  acceptance tests cannot all pass simultaneously, its scope spans two
  branches, or its `depends_on` was dishonest and it needs something not yet
  merged;
- **the right fix is outside the packet's scope** — the defect is real but
  correcting it means changing a shared pattern, a schema, or another
  module, which by code-standards §3.2 must be changed everywhere or not at
  all;
- **the builder escalated** and its argument has merit;
- **an acceptance test was modified**, whatever the justification;
- **a spec ambiguity has no safe narrowest behaviour** — every available
  option guesses.

A reviewer that only ever says `approve` or `rework` will grind a
mis-sliced packet through three rework cycles and then block it, having
spent three builder runs to discover something visible on the first read.
When the packet is the problem, say so on the first read.

## Verdict format

Return JSON through the structured-output channel requested by the controller.
You have no write-capable tool. Nothing else you say has any effect — **you do
not set a status.** The controller decides
what your verdict means, which is why `escalate` on a mis-sliced packet is
worth as much as a careful rework on a good one.

```json
{
  "verdict": "rework",
  "blocking": [
    "platform/claim_core/service.py:214 - catch-and-log around load_claim swallows a missing-claim bug; the caller then writes a version with a null policy ref. code-standards §1.2, PRD-00 §0.4.",
    "packs/motor/calcs.py:88 - excess boundary tested at 49_999_00 and 50_001_00 but not 50_000_00. PRD-07 §7.4 boundary is exact. code-standards §4.2."
  ],
  "non_blocking": ["platform/review_queue/api.py:31 - `resolve_item2` name."],
  "judgement_calls": [],
  "escalation": null
}
```

Rules the controller enforces, and will reject your verdict for breaking:

- `rework` with an empty `blocking` list is rejected — there is nothing for
  the builder to fix.
- `approve` with a non-empty `blocking` list is rejected — pick one.
- `escalate` with no `escalation` prose is rejected.
- A malformed or missing structured verdict escalates the packet to the owner. It
  does not default to anything.

What the controller does with each verdict:

| verdict | blast_radius | resulting status |
|---|---|---|
| approve | false | `merge_ready` (or `merged`, if two identities and auto-merge are configured) |
| approve | **true** | `escalated` — regardless of verdict |
| rework | any | `rework`, with your blocking findings passed verbatim to the builder |
| escalate | any | `escalated` |

Your `blocking` findings are the builder's next prompt. Write them so
someone with no memory of this review can act on each one: `file:line`, the
defect, the spec clause. "Fix the error handling" is not a finding.

A `blast_radius: true` packet escalates even when you approve it. Say
plainly in your findings that you approved it and it is escalated for its
blast radius, not for a defect — otherwise the owner reads the digest and
thinks something is wrong.

The append-only controller ledger and its audit branch are the record of why
something merged. If the owner cannot reconstruct your decision from those
plus git, you have not finished.
