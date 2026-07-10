## PRD-08 — Approval Pack Generator (build spec) ⭐

> **v1.1** — incorporates the CTO decision round of 2026-07. Where this document conflicts with Section 0 (Shared Engineering Decisions) or Section 0.5 (Shared Agent Runtime), those files win. Anything underdetermined: follow ED-11 (never decide locally — route to the open-items register).

### 8.1 Purpose

One pipeline, two artifacts from the claim object: the merged chronological PDF and the drafted Motor Approval Note — crash-proof, cited, routed. The wedge that converts ~30–40 manual minutes into < 2 min generation + ≤ 8 min review.

### 8.2 Trigger & readiness gate

Eligible when: FSM = RESERVED — which per PRD-00 §0.4 (v1.1) means **C-02/C-03 executed locally with verified inputs; projection is a parallel tracker, not a guard** (the former `pre_projection_pack` hedge is **deleted**) — chase checklist complete or outstanding items waived, all manifest-required fields ≥ their template verification floor. A **readiness card** on Claim 360 shows the manifest as a live checklist with per-item blockers — the explicit version of today's implicit completeness check.

### 8.3 Manifest & merge engine (capability `pack.merge`)

Pack-defined manifest, motor v1 (order = strict chronology of the claim, matching current practice):

|#|Item|Source|Conversion|
|---|---|---|---|
|1|Policy document/schedule|claim doc|passthrough|
|2|Intimation email|communications|HTML→PDF (headless Chromium, print CSS)|
|3|Claim form|claim doc|passthrough|
|4|Logbook|claim doc|passthrough|
|5|Driving licence|claim doc|passthrough|
|6|KRA PIN cert|claim doc|passthrough|
|7|Photos|photo_damage docs|2-up per A4 page, caption = filename + received date|
|8|Repair estimate|claim doc|passthrough|
|9|Assessor engagement email|communications|HTML→PDF|
|10|Assessor report|claim doc|passthrough|
|11|Supplier quote emails/breakdown|communications/docs|HTML→PDF each|
|12|Assessor payment request|`source: projection_readback \| upload` (v1.1 flag)|passthrough|
|13|Claim details report|`source: projection_readback \| upload` (v1.1 flag)|passthrough|

**Items 12–13 (v1.1):** these are ICON-emitted artifacts; the manifest flag makes them satisfiable either by the projection's captured artifact **or** by officer upload (paste-assist era: download from ICON, drag in — one action). The readiness gate reads the flag; **neither item is waivable**.

Engine: pypdf merge with **PDF bookmarks per item** (an upgrade approvers get free), cover page = manifest table {item, source document, received date, pages}.

**HTML→PDF policy (v1.1, binding — applies to every headless-Chromium render, here and in PRD-01 NORMALIZE):** network-disabled Chromium — request interception denies everything except `data:` URIs and CID-inline attachments; remote fonts/images blocked (privacy + determinism). A4 portrait, 18mm margins, fixed viewport, print CSS, **pinned Chromium version in the container image**, UTC render with an EAT timestamp header. 30s render ceiling → plaintext fallback render, flagged in the manifest. **Regenerating a pack must be byte-stable modulo the timestamp header** (acceptance-tested). Output: `All Docs merged for {reg}.pdf` (naming preserved — approvers recognise it), stored S3 with Object-Lock (immutable), sha256 + manifest JSON to ledger. Any missing non-waived item ⇒ generation refuses with the blocking list (StrictUndefined philosophy applied to files). Idempotent regeneration: new version, old versions retained.

### 8.4 Approval note T-01 — structure & generation

Three field classes, three generation rules — **this separation is the product's integrity model**:

**(a) Computed/merged fields (never generated):** amount payable = **calc C-08** (registered `blocked_on_inputs`; formula = top-priority CM capture, open item 5; until live, T-01 renders `PENDING CAPTURE` in this slot and **refuses sign-off** — PRD-02 §2.3/§2.4), repair amount, assessed amount, estimate, excess, PAV, % of SI, % of PAV, garage, loss location, third-party count, excess protector (bool), duty paid (bool ❓source — likely logbook/import status, capture at embed), recovery-register flag, subrogation (bool + basis). Rendered by Jinja from `claim_fields`; every figure carries a superscript citation marker in the note draft UI linking to its calc_run/citation.

**(b) Verification fields (deterministic checks, rendered as pass/flag lines):** driver age/experience vs DL (CC-2 result + computed values: "Driver 44 yrs, licence held 23 yrs — consistent with DL"), driver-is-insured (party match), logbook verification (CC-1/CC-4 results), narrative-photo consistency (CC-5 verdict verbatim, including `flagged` — the note must not launder a flag).

**(c) Commentary (constrained generation, `MODEL_HEAVY`):** incident summary (≤80 words), excess-vs-max commentary, supply-model savings narrative (garage 277,476 → Kawama 48,000 → claim 136,276, savings 91,200 — from savings_ledger rows). Prompt contract: input = a JSON bundle of **only** verified structured fields + ledger rows; instruction set (versioned prompt `pack.note_commentary@v1`): _use no number absent from input; no adjectives implying judgment of liability; British English; sections exactly as templated_. Output schema per paragraph includes `numbers_used[]` — post-processor asserts every number ∈ input set (fail ⇒ regenerate once ⇒ else `EXCEPTION`). Grader `G-NOTE` independently re-extracts numbers from prose and diffs.

### 8.5 Draft persistence & review (the W-09 kill)

sql

```sql
CREATE TABLE note_drafts (
  id TEXT PRIMARY KEY, claim_id TEXT NOT NULL, version INT NOT NULL,
  body JSONB NOT NULL,              -- structured: sections[], each {template_slot, content, locked}
  status TEXT NOT NULL,             -- 'draft'|'in_review'|'signed'|'superseded'
  edited_by TEXT, signed_by TEXT, signed_at TIMESTAMPTZ,
  UNIQUE (claim_id, version)
);
```

Console `NOTE_REVIEW` workspace (extends S-1): left = note editor — computed fields **locked** (read-only, citation superscripts), commentary editable with tracked diff; right = merged PDF viewer. Autosave every 5s to a new draft version (nothing is ever lost — the sold feature). Officer **Sign** action: writes `signed_by`, freezes the version, renders final PDF artifact, transitions FSM → PACK_READY → IN_APPROVAL, routes per authority matrix (PRD-02 §2.5), creates the approval item in the right manager's S-3 queue, fires R-12/T-03 when > 4M. Manager Reject returns to PACK_READY with structured reasons → new draft version pre-seeded with reasons; every rejection auto-captures a `production_correction` test case (3.5).

ICON transport at launch: signed note renders additionally as a **paste-assist field set** (PRD-09 §9.4) ordered to ICON's note form; RPA transport supersedes it later — same artifact, different adapter mode.

### 8.6 Capabilities

`pack.merge` (max L4; launch L1 → L3 fast; merge is deterministic) · `pack.note_draft` (max **L3** — a signed human name goes on every note entering the approval chain, permanently; L3 means the _draft_ is generated + auto-queued without an officer requesting it, sign remains human) · `pack.route` (max L4; pure matrix lookup).

### 8.7 Acceptance

(1) Reference claim fixture → merged PDF matches manifest order with bookmarks; cover manifest complete; sha in ledger; (2) delete a required doc → generation refuses, names the gap; (3) note draft: all class-(a) figures byte-match calc_runs; G-NOTE catches a deliberately-injected wrong number in commentary (red-team test in CI); (4) kill the browser mid-edit → reopen shows ≤5s of loss; (5) 4.1M claim → routes to MD + T-03 rendered with all 16 fields; (6) timed trial with the officer: generation < 2 min, review+sign ≤ 8 min on a standard repair; (7) approver-side: pack opens in S-3 with zero layout regressions vs the PDF they receive today (side-by-side sign-off with CM).