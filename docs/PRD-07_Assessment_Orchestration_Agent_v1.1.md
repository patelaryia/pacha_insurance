## PRD-07 — Assessment Orchestration Agent (build spec)

> **v1.1** — incorporates the CTO decision round of 2026-07. Where this document conflicts with Section 0 (Shared Engineering Decisions) or Section 0.5 (Shared Agent Runtime), those files win. Anything underdetermined: follow ED-11 (never decide locally — route to the open-items register).

### 7.1 Purpose

Estimate → assessment routing → assessor dispatch → report parsing → downstream cascade (write-off gate, reserves, savings ledger). Builds the outcome-pricing dataset.

### 7.2 Vendor registry

`vendors(id, kind: assessor|garage|supplier|salvage_yard, name, emails[], fee_schedule JSONB, active)` — seeded at embed: assessor firms + standard fees (physical KES 6,380 observed; desk 0; re-inspection 2,900 ❓confirm distinct fee schedule per firm). Assessor selection v1 = officer picks from registry in the dispatch confirm item; auto-rotation is out of scope until data exists.

### 7.3 Mode decision (desk vs physical) — dual-path, resolves ODQ-6

On `assessment.estimate_total` verified:

- **Path A (authoritative):** rule R-06 threshold compare → recommendation. Until Q-02 lands, rule sits `blocked_on_inputs` and the mode item goes to the officer undetermined (they choose; their choice is _labelled training data_).
- **Path B (shadow, L0 permanently until re-decided):** `MODEL_HEAVY` with inputs {estimate total + line items, damage photos, narrative, vehicle age} → `{mode, rationale, confidence}`. Logged to `agent_runs`, **never surfaced**. Weekly eval compares Path B vs officer decisions; if after ≥100 decisions agreement ≥ 95% where the rule and officer diverge, we revisit — a deliberate future decision, not a drift.

Officer confirms via `MODE_CONFIRM` review item (shows estimate, threshold verdict, photos strip). Capability `assessment.mode_confirm`, **max L3** (L3 = auto-apply rule verdict with 10% sampling once R-06 value confirmed + 50 clean confirms).

### 7.4 Dispatch (capability `assessment.dispatch`)

Template `T-11` (❓capture verbatim at embed; interim: reconstructed from observed emails) — merge: claim ref, insured, reg, loss summary, mode, garage details; **cc broker** (current practice, keep); attachments = doc-pack subset {claim form, estimate, photos, logbook} assembled from S3. Multi-assessor mode (R-07): officer may select N firms in the confirm item → N sends, `assessment.multi_mode=true`. Launch L1 → L2; L3 after 25 clean.

SLA `assessor_turnaround` starts at send (warn 3d, breach 5d); reminder to assessor at warn via chase engine (a one-item checklist `purpose='assessor_report'` — reuse, don't rebuild).

### 7.5 Report ingestion & cascade

Assessor reply enters via PRD-05 thread-match → PRD-01 extracts `assessor_report` schema. On fields verified (financial fields ≥ 0.90 or human-verified):

```
C1 write_off_gate   R-05 evaluate → true: FSM → WRITE_OFF (hand-off PRD-11)
C2 selection        multi-assessor: wait-all (timeout = SLA breach → proceed-with-
                    received via officer confirm); build comparison artifact
                    (table: firm × {agreed_quote, pav, fee, flags}); R-07 selects
                    lowest agreed_quote → EXCEPTION review if officer overrides
C3 reserves         C-02, C-03 execute → reserve.* fields (G-CALC, G-SUM run);
                    projection request emitted (PRD-09 consumes when live)
C4 savings          C-05: header delta (estimate_total − agreed_quote) + per
                    supplier_line delta → savings_ledger rows (7.6)
C5 flags            assessor flags[] + CC-1..CC-5 results → CONSISTENCY_FLAG
                    review items (capability ceiling L2 — D-04 stays human)
```

### 7.6 Savings ledger

sql

```sql
CREATE TABLE savings_ledger (
  id TEXT PRIMARY KEY, claim_id TEXT NOT NULL,
  kind TEXT NOT NULL,               -- 'assessment_negotiation'|'supplier_substitution'|
                                    -- 'salvage_recovery' (PRD-11 writes)
  baseline_amount BIGINT NOT NULL, achieved_amount BIGINT NOT NULL,
  saving BIGINT GENERATED ALWAYS AS (baseline_amount - achieved_amount) STORED,
  evidence JSONB NOT NULL,          -- calc_run ids + document citations
  vendor_id TEXT, occurred_at TIMESTAMPTZ NOT NULL
);
```

**Ledger semantics (v1.1, binding — these, not the anecdote, are what agents build against):**
- Header rows (`kind='assessment_negotiation'`, baseline = `assessment.estimate_total`, achieved = `assessment.agreed_quote`) are the **billable** measure.
- Line rows (`kind='supplier_substitution'`, baseline = garage line price, achieved = supplier price) are decomposition **evidence**.
- Reporting sums **header rows only**. No arithmetic invariant links lines to header (labour deltas make them non-additive), so double-counting is structurally impossible.

**Canonical acceptance fixture FX-1 (v1.1 — replaces the prior corrupted example, which is struck):**

| row | kind | baseline (cents) | achieved (cents) | saving | vendor |
|---|---|---|---|---|---|
| 1 | assessment_negotiation | 26_100_000 | 13_627_600 | **12_472_400** (KES 124,724) | — |
| 2 | supplier_substitution | 13_920_000 | 4_800_000 | **9_120_000** (KES 91,200) | Kawama |

(FX-1 reconciles the embed numbers on a 139,200 garage door line; the previously quoted 277,476 figure is under re-verification with Gilbert — open item 14 — but FX-1 is canonical regardless.) Every row must carry citation-bearing evidence — this ledger is contract-billable, so it inherits critical grader `G-SUM` + citation checks.

### 7.7 Acceptance

(1) Corpus standard-repair claim end-to-end: estimate in → mode item → dispatch draft → (inject assessor reply) → report parsed, reserve computed, savings row written with citations, consistency flags raised — reserve figures byte-match the closed claim's actuals; (2) write-off corpus case crosses R-05 at exactly >50% (boundary test) → WRITE_OFF transition; (3) multi-assessor with one non-responder → SLA breach → proceed-with-received flow; (4) shadow-mode Path B produces zero user-visible surface (UI snapshot test); (5) savings MTD tile reconciles to Σ **header rows only** (per §7.6 semantics), verified against FX-1.