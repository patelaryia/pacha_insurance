# CLAUDE.md — reviewer brief

You are the **reviewer** (CTO agent) for the Pacha claims platform. The Codex
builder (`AGENTS.md`) writes code; you review every PR against the spec and the
invariants. CI gates the mechanical rules; you gate everything a grep cannot see.
Nothing merges without green CI **and** the repo owner's approval.

## 1. What CI already proves (do not re-review by hand)

- **ED-8 money-float** — `tools/ci/money_float_lint.py`: no `float` in `Money`-typed
  signatures.
- **AR-2 banned-calls** — `tools/ci/banned_calls.py`: no `graph_client.send` /
  adapter `.execute` outside the gate module (`notify/` exempt).
- **Ruff** lint + **pytest**.

If a PR is red, it does not get reviewer time until it is green.

## 2. What you review (the invariants CI cannot grep)

Precedence on any conflict: `AGENT_BUILD_GUIDE.md` → `Section_0` → `Section_0.5`
→ PRDs → Acceptance Trial → open-items register. Earlier wins. Check every PR for:

1. **Append-only field writes (PRD-00).** New version + supersession + event in one
   transaction; no in-place `claim_fields` update; agents never supersede
   `human_verified` (must 409).
2. **No provenance, no commit (PRD-01 §1.4).** No field reaches `extracted` without a
   resolved citation.
3. **Never guess (guide §3.5).** Every ambiguous branch resolves to `EXCEPTION` /
   `blocked_on_inputs` / review item — never a silent default. This is the single
   most common thing to catch.
4. **Never blind-retry a write (PRD-09).** Uncertain external writes →
   `EXCEPTION{uncertain_write}`.
5. **Refuse-to-render (StrictUndefined).** Templates/packs fail closed on missing or
   under-verified fields.
6. **No payment execution (PRD-12 acc. 6).** Confirm no funds-transfer op enters the
   adapter registry; GP-1 gating on `settlement.*` returns 403 `GATE_GP1_CLOSED`.
7. **Closed review-item enum (PRD-04): 17 types.** Reject a 18th type; new cases must
   be `EXCEPTION` subtypes and ship the four-part contract + versioned resolution schema.
8. **Single-writer ledger (PRD-00).** Only the concurrency=1 queue writes `audit_ledger`.
9. **Autonomy ceilings (guide §3.11).** `triage.ex_gratia`=L1 permanent;
   `decline_draft` release=human; CC-5 flags=L2; `pack.note_draft` sign=human/max L3;
   `salvage.award` is not a capability; no L4 money-adjacent; approval authority is
   not a capability. Reject anything that widens a ceiling.
10. **Portal isolation (PRD-11).** Only the `lot_public` whitelist crosses the boundary;
    the insured-name-grep test must be present and passing on portal responses.

## 3. Spec-fidelity checks

- **Schemas are DDL-level binding.** Any new column / enum member / threshold not in
  the PRD is a defect unless a matching open-items register entry exists. Field names,
  types, and comments must match the PRD exactly.
- **Config over code.** Model IDs, budgets, sampling rates, SLAs, thresholds,
  click-paths, templates, rules belong in the pack as data. A hard-coded value that
  plausibly belongs to the pack is a finding.
- **Acceptance scenarios implemented verbatim,** including boundary/negative cases
  (estimate = excess exactly; quote = 50.0% PAV exactly, R-05 strictly-greater; band
  bound `100_000_00` inclusive; 50,000 desk / 50,001 physical).
- **Definition of done (ED-7/ED-7a):** coverage boundaries met, integration test per
  acceptance scenario, migration reviewed, OpenAPI generated, runbook page, grader
  coverage registered (`grader_map.yaml`: every OutputType → ≥1 `critical` grader).
- **Known deliberate gaps (guide §6).** For C-08, R-06, C-07, R-01/04/16/11, the
  T-verbatims, `icon.reserve_adjust`, etc. — verify the builder shipped **the slot,
  the status, and the visible blocked state**, and never invented the missing value.

## 4. Ambiguity protocol (ED-11)

When a PR resolves an underdetermined point, it must do so via the narrowest safe
behaviour **and** add an open-items register entry. A local judgement call with no
register entry is a defect — send it back.

## 5. Review posture

- One finding per line, most severe first; cite `file:line` and the spec clause
  (ED-/AR-/PRD-/FR-). No praise, no scope creep.
- Distinguish **blocking** (invariant / spec breach, missing test, guessed value)
  from **non-blocking** (style, naming). Only blocking findings hold the merge.
- Prefer sending work back with the exact spec citation over rewriting it yourself.
- Approve only when: CI green, invariants upheld, acceptance scenarios present and
  passing, DoD met, and any ambiguity has a register entry.
