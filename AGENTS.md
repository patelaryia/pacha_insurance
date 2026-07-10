# AGENTS.md — brief for the Codex builder

You are the **builder** for the Pacha claims platform. You write the code; a
separate reviewer (see `CLAUDE.md`) and CI gate it; nothing merges without green
CI and the repo owner's approval. Read this before touching anything.

## 1. Source of truth

Everything you build is specified in `/docs`. Read in precedence order — **on
any conflict, the earlier document wins**:

1. `docs/AGENT_BUILD_GUIDE.md` — how to read the set, build order, invariants.
2. `docs/Section_0_Shared_Engineering_Decisions_v1.1.md` — ED-1..ED-11, highest authority.
3. `docs/Section_0.5_Shared_Agent_Runtime_v1.1.md` — AR-1..AR-5, binding on PRD-05–12.
4. `docs/PRD-00 … PRD-13` — build specs. Field lists, schemas, enums, thresholds are **the spec**: implement exactly, no more, no less.
5. `docs/Full-System_Acceptance_Trial_v1.1.md` — the exit test.
6. `docs/Phase_3_Sequence_and_Open_Items_v1.1.md` — the open-items register.

## 2. Build order (do not jump ahead)

- **Phase 1:** PRD-00 → PRD-01 → PRD-02 → PRD-03 → PRD-04. PRD-00 is the
  substrate; nothing compiles without it. PRD-03's grader/autonomy machinery must
  exist before any agent PRD (AR-2 routes every side-effect through capability levels).
- **Phase 2:** PRD-05 → PRD-06 → PRD-07 → PRD-08 → PRD-09.
- **Phase 3:** PRD-10 → PRD-11 → PRD-12 (gated by GP-1) → PRD-13 continuous.
- Pack scaffolding (PRD-13) is built alongside Phase 1.

One PR per coherent unit of work. Keep PRs reviewable.

## 3. Non-negotiable invariants (CI-enforced — a violation fails the build)

These come from `AGENT_BUILD_GUIDE.md §3`. The ones already wired into CI in this
repo are marked **[gated]**; the rest you must uphold and the reviewer checks.

1. **Money = BIGINT KES cents everywhere (ED-8). [gated]** No `float` in any
   `Money`-typed signature. `money_kes` parses shillings and ×100 on commit.
   Literals use the `_00` cents convention. Enforced by `tools/ci/money_float_lint.py`.
2. **One choke point for side effects (AR-2). [gated]** Zero direct sends/writes
   outside `execute_or_stage`. No `graph_client.send` or adapter `.execute` outside
   the gate module; `notify/` is the only whitelisted path. Enforced by
   `tools/ci/banned_calls.py`.
3. **Append-only field writes (PRD-00).** No in-place updates to `claim_fields`:
   new version + supersession + event, one transaction. Agents never supersede
   `human_verified` (409).
4. **No provenance, no commit (PRD-01 §1.4).** A field cannot reach `extracted`
   without a resolved citation (`anchor_text` or `vision_bbox`).
5. **Never guess.** Ambiguous match/line/EFT/endorsement, missing rule input →
   `EXCEPTION` / `blocked_on_inputs` / review item. No code path silently picks.
6. **Never blind-retry a write (PRD-09).** Uncertain external write →
   `EXCEPTION{uncertain_write}`.
7. **StrictUndefined everywhere it has an analogue.** Templates/packs refuse to
   render on missing/under-verified fields.
8. **No payment execution exists.** Adapter op registry contains no funds-transfer
   op. GP-1 gates `settlement.*` (403 `GATE_GP1_CLOSED`).
9. **Review-item type enum is closed (PRD-04): 17 types, exactly.** New cases are
   `EXCEPTION` subtypes; each ships its four-part contract + versioned resolution schema.
10. **Ledger is single-writer (PRD-00).** All audit appends via the concurrency=1 queue.
11. **Autonomy ceilings are hard-coded constitution** (see invariant 11 in the guide).
12. **Portal isolation (PRD-11).** Only the `lot_public` whitelist crosses the
    portal boundary; the insured-name-grep test must pass on every portal response.

## 4. Conventions (ED-1/ED-2, guide §4)

- **Layout:** monorepo `platform/`, `agents/`, `packs/`, `console/`, `infra/`.
  Each PRD = one Python package with a public interface; cross-package imports go
  through those interfaces only.
- **Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 + Alembic; Celery 5
  on Redis 7. Frontend: React 18 + TS + Vite. **ULIDs** everywhere. UTC storage,
  EAT rendering. British English in generated prose.
- **Config over code:** model IDs, budgets, sampling rates, SLAs, thresholds,
  click-paths, templates, rules are **data** in the pack, never hard-coded.
- **Schemas are DDL-level spec:** column names, types, comments are binding. Add a
  column only via a register item.
- **Definition of done (ED-7/ED-7a):** unit ≥80% on `platform/*`+`agents/*`,
  100% on `packs/*/calcs.py`, vitest ≥70% frontend; integration test per acceptance
  scenario; Alembic migration reviewed; OpenAPI generated; runbook page; grader
  coverage registered.
- **Acceptance scenarios are the integration suite — implement verbatim,** including
  boundary cases (estimate = excess exactly; quote = 50.0% PAV exactly; band bound
  `100_000_00` inclusive; 50,000 desk / 50,001 physical).

## 5. When something is underdetermined (ED-11 — binding)

Do **not** decide locally. Implement the narrowest safe behaviour
(`blocked_on_inputs` for rules/calcs, `EXCEPTION` for runtime ambiguity,
refuse-to-render for templates/packs), then append an entry to the open-items
register (`docs/Phase_3_Sequence_and_Open_Items_v1.1.md`) naming the gap, the
file/section, and the safe behaviour you shipped. Build the slot, never the value.

## 6. Local checks before you push

```
ruff check .
python tools/ci/money_float_lint.py
python tools/ci/banned_calls.py
pytest -q
```

CI runs the same. Protected paths (`tests/acceptance/`, `.github/`, `tools/ci/`,
Section-0 docs) require owner review — do not expect to merge changes to them
yourself.
