## PRD-04 — Status Console & Review Queue (build spec)

> **v1.1** — incorporates the CTO decision round of 2026-07. Where this document conflicts with Section 0 (Shared Engineering Decisions) or Section 0.5 (Shared Agent Runtime), those files win. Anything underdetermined: follow ED-11 (never decide locally — route to the open-items register).

### 4.1 Purpose

The single human surface: officers work a queue instead of an inbox; managers see the portfolio; status is loud (the W-10 answer). React SPA against the platform API, Entra ID SSO.

### 4.2 Roles (RBAC, enforced server-side per route + row)

`claims_officer` (queue, claim 360, release L1 drafts) · `asst_claims_manager` / `claims_manager` / `gm` / `md` / `chairman` (approval views per matrix band; CM additionally: autonomy sign-off, corpus admin) · **`head_of_claims`** (v1.1: salvage committee member, T-03 recipient, portfolio-dashboard access; **no approval band by default** — band assignment is Mayfair org config; whether HOC ≡ CM at Mayfair is open item 13) · `finance` (payment-adjacent views, read) · `admin` (pack versions, users, adapters) · `auditor` (read-only everything incl. ledger). A user's approval band comes from role; the console never lets anyone approve outside band (server 403).

### 4.3 Screens (build exactly these six)

**S-1 Review Queue** (officer home). Left: filterable list of `review_items`.

**Review-item type enum — FINAL AND CLOSED (v1.1). Do not add types; new cases are `EXCEPTION` subtypes:**
`FIELD_VERIFY, DOC_CLASSIFY, DOC_SPLIT, CONSISTENCY_FLAG, DRAFT_RELEASE, MODE_CONFIRM, NOTE_REVIEW, PACK_REVIEW, EX_GRATIA, EXCEPTION, PROMOTION_SIGNOFF, SAMPLE_REVIEW, PASTE_READBACK_CHECK, PROCEED_PARTIAL, KYC_VERIFY, EFT_MATCH, REOPEN_PROMPT`
(`budget_exceeded`, `legacy_unmatched` and similar are `EXCEPTION` subtypes, not new types.)

**Four-part contract (mandatory for every type):** each type ships with (1) producing event(s), (2) workspace layout, (3) resolution actions, (4) a **versioned resolution-payload JSON schema** — the payload schemas are consumed as training data by PRD-03 §3.5, so schema changes are versioned like prompts.

**Queue routing (v1.1 assignment model):** items default-route to the claim's owning officer (`claims.assigned_to`, assignment per PRD-05 §5.8); an "all items" pool view lets any officer work anything — resolution always logs the actual actor. Right: item workspace. Every item shows agent output + citations + exactly three primary actions: **Approve / Edit→Approve / Reject (reason required, enum + free text)**.

**Keyboard (v1.1 focus rules):** `a` approve, `e` edit, `r` reject, `j/k` navigate — implemented as roving focus on the list/item container, **disabled whenever focus is in any input/textarea/contenteditable**; `a/e/r` require an explicitly focused item; `Esc` returns focus to the list. No global keydown handlers. Item SLA chip. Resolution writes `review.resolved` with structured diff (feeds 3.5). Target interaction cost: FIELD_VERIFY ≤ 10s, NOTE_REVIEW ≤ 8min.

**S-2 Claim 360.** Header: claim id, insured, reg, amount, **status rail** (FSM states as a horizontal stepper; DECLINED renders as a full-width red banner with reopen action — declines are structurally impossible to miss). Tabs: _Overview_ (parties, key fields with confidence/verification badges), _Documents_ (checklist tracker: requested/received/verified/waived per item, chase history, per-item age), _Fields & Citations_ (table of current fields; clicking any field opens **the citation viewer**: pdf.js page render, bbox highlight, value panel, verify/correct inline), _Financials_ (estimate vs agreed vs reserve breakdown vs savings, each figure linked to its calc_run), _Timeline_ (event stream, human-readable), _Systems_ (projection status per external system: pending/completed/diverged with detail), _Communications_ (threaded).

**S-3 Approval Workspace** (managers). Queue filtered to their band; opens the merged PDF (inline viewer) + drafted note side by side; Approve / Reject-with-reasons; >4M shows T-03 alert status. Deliberately mirrors what approvers see today — zero retraining.

**S-4 Portfolio Dashboard** (CM+). Tiles: open claims by state; SLA breaches (click-through); autonomy + no-touch rate trend; savings ledger MTD/YTD; median active-handling time; aging histogram; per-officer queue depth. Every tile query is a saved, exportable series (CSV) — this is the outcome-pricing evidence pack.

**S-5 SLA Board.** All open clocks sorted by breach proximity; bulk escalate.

**S-6 Admin.** Pack version viewer (rules/calcs/templates with diffs), capability table with promotion evidence + sign-off flow (two-person where policy requires), adapter health, user/role management, audit-ledger search.

### 4.4 Notifications

In-app (websocket) + email: immediate for `sla.breached`, `projection.diverged`, `grader.failed(critical)`, `autonomy.demoted`; daily 08:00 EAT digest per officer (**digest = owned claims**, per the assignment model); escalation emails follow `escalate_to_role`. All notification sends ledgered.

**Transport (v1.1):** all staff notifications go through the `notify` module (Section 0.5 AR-5) — direct Graph send permitted, recipients restricted to the allowlisted staff domain, **exempt from G-COMM and the autonomy gate**, still ledgered. The AR-2 CI grep whitelists `notify/` by path.

### 4.5 NFRs & acceptance

Queue and Claim 360 p95 < 400ms; citation viewer renders page < 1.5s; works on 1366×768 (Mayfair desktops); Chrome/Edge. **Acceptance scenarios:** (1) officer processes a synthetic claim end-to-end touching only the console — inbox never opened; (2) decline on a claim is visible from queue, 360, and portfolio within 5s of the event; (3) FIELD_VERIFY median handle time ≤ 10s in a 20-item timed test; (4) approval attempted outside band → 403 + ledger entry; (5) citation click highlights the exact bbox for 50 sampled fields with zero misses; (6) reject-with-reason round-trips into a production_correction test case.