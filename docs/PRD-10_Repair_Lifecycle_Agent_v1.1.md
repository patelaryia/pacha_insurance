## PRD-10 — Repair Lifecycle Agent (build spec)

> **v1.1** — incorporates the CTO decision round of 2026-07. Where this document conflicts with Section 0 (Shared Engineering Decisions) or Section 0.5 (Shared Agent Runtime), those files win. Anything underdetermined: follow ED-11 (never decide locally — route to the open-items register).

### 10.1 Purpose

APPROVED → authority letter + LPOs → completion detection → re-inspection routing → release → invoice reconciliation. Almost entirely `MECH`/`DET`; the only permanent human touchpoints are invoice mismatches and failed re-inspections.

### 10.2 Data model

sql

```sql
CREATE TABLE repair_orders (
  id TEXT PRIMARY KEY, claim_id TEXT NOT NULL,
  garage_party_id TEXT NOT NULL,
  status TEXT NOT NULL,            -- 'authorized'|'in_repair'|'completion_reported'|
                                   -- 'reinspection'|'released'|'closed'
  authority_letter_ref TEXT,       -- artifact s3 key
  lpo_refs JSONB DEFAULT '[]',     -- one LPO artifact per supplier line in C-03
  authorized_at TIMESTAMPTZ, completion_reported_at TIMESTAMPTZ, released_at TIMESTAMPTZ
);
CREATE TABLE invoices (
  id TEXT PRIMARY KEY, claim_id TEXT NOT NULL, vendor_id TEXT NOT NULL,
  kind TEXT NOT NULL,              -- 'garage'|'assessor'|'supplier'|'towing'
  document_id TEXT NOT NULL REFERENCES documents(id),
  invoice_no TEXT, amount BIGINT NOT NULL,
  matched_line_id TEXT,            -- FK reserve breakdown line (C-03)
  match_status TEXT NOT NULL,      -- 'matched'|'matched_under'|'over'|'unmatched'
  resolved_by TEXT, occurred_at TIMESTAMPTZ NOT NULL
);
```

### 10.3 COP steps (capability set `repair.*`)

```
S1 authorize      on FSM→APPROVED: render T-08 Repair Authority Letter (garage) +
                  one LPO per supplier line in the C-03 breakdown (T-08b) — both
                  ❓verbatim capture (Q-06); merge fields locked to verified/calc
                  sources per PRD-02 floors. Send via AR-3. SLA repair_duration
                  starts (warn 14d, pack config).
S2 completion     inbound garage email (thread-match via PRD-05 router) →
                  MODEL_LIGHT classifies {complete | partial | query | other},
                  conf ≥0.85 else review. 'complete' → status completion_reported,
                  SLA stops. 'partial'/'query' → timeline note + officer item.
S3 route          R-08: physical re-inspection if agreed_quote > 50_000_00 OR
                  parts_replaced (defined: assessment.supplier_lines non-empty);
                  strictly-greater — exactly 50,000 routes desk (boundary in CI).
                  R-09: validation assessor ≠ initial (vendor registry compare;
                  violation = hard block, re-select).
S4 dispatch       reuse PRD-07 §7.4 dispatch with purpose='reinspection', fee from
                  vendor fee_schedule (observed 2,900 ❓per-firm confirm). Desk mode:
                  request is a photo-pack email to the same flow.
S5 verdict        new doc schema `reinspection_report` {satisfactory: bool,
                  parts_confirmed: bool, notes, photos_ok} → register in PRD-01.
                  satisfactory=false → EXCEPTION{type: reinspection_failed};
                  rectification loop is officer-driven v1 (agent tracks, human talks).
S6 release        render T-09 release note → garage (authorises invoicing).
                  FSM → RELEASED.
S7 reconcile      each inbound invoice → schema `repair_invoice` (= repair_estimate
                  + invoice_no) → match against C-03 lines by (vendor, category):
                  amount == line → matched; < line → matched_under (residual reserve
                  release computed; projected via icon.reserve_adjust once its
                  click-path is captured — until then a console task prompts the
                  officer, ledgered, per PRD-09 v1.1); > line → 'over' →
                  EXCEPTION{type: invoice_mismatch} showing invoice citation vs
                  reserve line vs assessor report — permanently human.
                  MATCH AMBIGUITY (v1.1): two C-03 lines share (vendor, category)
                  → EXCEPTION{type: invoice_ambiguous} — never guess.
                  All lines matched/resolved → registered field
                  `repair.payment_ready: bool` set true (the formal PRD-12 S1
                  trigger contract for repair-path claims, v1.1).
```

### 10.4 Capabilities

|capability|max|launch|notes|
|---|---|---|---|
|`repair.authorize`|L4|L1|letters carry approved figures only|
|`repair.completion_detect`|L4|L1|misclassification = major, self-corrects at S5|
|`repair.reinspection_route`|L4|**L3**|pure `DET` (R-08/R-09) — fast-track with sampling|
|`repair.release`|L3|L1|authorises invoicing → sampled review permanent|
|`repair.invoice_match`|L3|L1|'over' outcomes human forever|

### 10.5 Acceptance

(1) Corpus standard repair replays S1→S7 with reserve-line invoices → all matched, residual computed correctly to the cent; (2) boundary: quote exactly 50,000 → desk; 50,001 → physical; (3) same-assessor re-inspection attempt → blocked; (4) over-invoice fixture (garage bills 140,000 vs line 136,276) → exception with three-way evidence view; (5) partial-completion email → no release note exists (negative assertion on artifacts); (6) SLA warn fires at day 14.

---