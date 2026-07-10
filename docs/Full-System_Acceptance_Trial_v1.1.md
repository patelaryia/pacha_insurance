## Full-System Acceptance — the Analyst-Equivalence Trial (v1.1)

> **v1.1** — restructured per the CTO decision round: 8-week live run (4-week ramp + 4-week measured trial), re-key metric restated, rework defined, volume corrected to ~6 claims/working day (823 YTD ÷ ~27 weeks).

This is the exit test for the entire build, run at Mayfair once R2 scope is live.

### Structure (v1.1)

**Eight-week live run: weeks 1–4 ramp, weeks 5–8 measured trial.**

- All new motor intimations dual-tracked from ramp day 1 (platform processes; officer reviews via console only — inbox untouched by protocol).
- **Promotion counters accrue from ramp day 1**; ramp claims are dual-tracked identically to trial claims. At ~6 claims/working day, the ramp yields ~120 claims; high-frequency capabilities (acknowledge, doc-request, chase reminders) accrue *multiple* items per claim, so the 25/50-item promotion ladders are reachable by weeks 4–5 for the capabilities the trial requires at L3+.
- All pass thresholds are measured at week-8 close **over weeks 5–8 only**.
- **Legacy book:** at ramp day 0, stub claims are imported for the open legacy book (reg, insured, ICON claim no, broker — CSV from ICON if exportable per open item 15, else officers key ~6 fields each, once). Thread/reference matching then lands legacy replies on real claim shells with status rails; genuinely unmatched mail routes `claim_related_unmatched` to the console. "Inbox untouched" survives day one, and officers get the status console for their whole book — a trial feature, not overhead.

### Pass criteria (measured from platform data — no self-reporting)

| Metric | Baseline (embed) | Pass threshold (weeks 5–8) |
|---|---|---|
| Intimation → acknowledgement | 8 days | < 15 min, 100% of claims (24/7 calendar — L3 ack runs round the clock and the metric counts it) |
| Median active handling / standard claim | ~60 min | ≤ 10 min (review-only) |
| **Manual field-entry sessions per claim into external systems** (v1.1 restatement of "re-keys") | 3–4 systems | **≤ 1 per claim while any live operation runs paste-assist** (the single consolidated ICON strip); **0 for any operation once its RPA mode is at L2+**. With EDMS on RPA during the trial, expected trial value = 1 |
| Extraction accuracy, financial fields | — | ≥ 98% vs human verification |
| Projection divergences | n/a | 0 sustained |
| Capabilities at L3+ | 0 | ≥ 6 incl. acknowledge, doc_request, chase.reminder, pack.merge |
| Savings-ledger capture | forwarded emails | 100% of assessed claims, cited (header-row semantics per PRD-07 §7.6 v1.1) |
| **First-pass approval rate on packs** (v1.1 restatement of "approver rework ≤ 5%") | — | **≥ 95%.** Rework = a manager **Reject** event on a first-submitted signed pack. Pre-sign officer edits are the review process working as designed; approver annotations without reject do not count |
| Lost drafts / crashed notes | "common" (W-09) | 0 |

Passing this trial *is* the pre-seed traction narrative and the outcome-pricing baseline: cost-per-claim and settlement-time deltas fall directly out of the SLA and chase tables. The "replacement" claim is then made precisely: every MECH/DET step runs at L2+ with a promotion trajectory to L3/L4; the residual human role is minutes of judgment per claim, absorbable by one senior handler across the current four-officer volume.
