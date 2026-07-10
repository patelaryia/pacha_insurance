## PRD-09 — System Projection & Reconciliation (build spec)

> **v1.1** — incorporates the CTO decision round of 2026-07. Where this document conflicts with Section 0 (Shared Engineering Decisions) or Section 0.5 (Shared Agent Runtime), those files win. Anything underdetermined: follow ED-11 (never decide locally — route to the open-items register).

### 9.1 Purpose

Move claim data into ICON and EDMS Powerhub without re-keying, capture readbacks (claim number), and guarantee zero silent divergence. Three adapter modes per operation, independently upgradeable.

### 9.2 Adapter framework

python

```python
class Adapter(ABC):
    system: str                      # 'icon'|'edms'|'finance'
    def health(self) -> AdapterHealth: ...
    def execute(self, op: Operation, payload: dict, run_id: str) -> OpResult: ...
    def readback(self, op: Operation, keys: dict) -> dict: ...
```

Registered operations v1: `icon.policy_read`, `icon.claim_register`, `icon.reserve_create`, `icon.reserve_breakdown`, **`icon.reserve_adjust`** (v1.1: residual reserve release for `matched_under` invoices — status `pending_capture`, click-path capture appended to open item 3; until captured, residual release is officer-manual via a console task at `matched_under`, ledgered), **`icon.assessor_payment_request`** (v1.1: replicates the existing straight-through as-is step generated at reserving; grouped with the reserve ops; **not behind GP-1** but capped **L3 with permanent sampling**), `icon.note_entry`, `icon.claim_details_report`, `icon.salvage_register` (PRD-11), `icon.payment_voucher` (PRD-12), `edms.general_payments`, `edms.claims_workflow`, `edms.attach_and_tag`, `edms.claim_payment`, `edms.payment_workflow` (PRD-12). Config maps `(operation) → mode ∈ {paste_assist, rpa, api}` — hot-swappable per operation; `EXCEPTION` fallback to paste_assist is always legal.

sql

```sql
CREATE TABLE projections (
  id TEXT PRIMARY KEY, claim_id TEXT NOT NULL, operation TEXT NOT NULL,
  mode TEXT NOT NULL, status TEXT NOT NULL,   -- 'queued'|'executing'|'verifying'|
                                              -- 'completed'|'failed'|'diverged'
  payload JSONB NOT NULL,                     -- field paths + values + versions (snapshot)
  readback JSONB, divergence JSONB,
  evidence JSONB,                             -- screenshot s3 keys per step (rpa),
                                              -- confirm ts + user (paste_assist)
  attempts INT DEFAULT 0, idempotency_key TEXT UNIQUE,
  created_at TIMESTAMPTZ, completed_at TIMESTAMPTZ
);
```

Idempotency key = `(claim_id, operation, payload_hash)` — a retried job can never double-register a claim or double-enter a reserve. Any operation that cannot verify its own prior completion on retry (e.g., crash after ICON submit, before readback) goes to `EXCEPTION{type: uncertain_write}` for human check — **never blind-retry a write**.

### 9.3 Mode 1 — Paste-assist (ships week 1 of Phase 2, works forever)

Console surface (extends S-2 Systems tab): per pending operation, a **field strip** ordered exactly to the target form's entry sequence (from the click-path spec, 9.5 — same source of truth), each field a copy button (copies raw value, no formatting), grouped by form screen, with per-group "done" checkboxes. Readback capture inline: after `icon.claim_register`, the strip's final input is "ICON claim number" → writes the **canonical field `external.icon.claim_no`** in `claim_fields` (source `projection_readback`, full provenance per PRD-00 §0.2 v1.1; the `claims.external_refs` cache updates via its dedicated consumer) + completes the projection. Confirm requires ticking a "values entered as shown" attestation (ledgered). Measured: per-operation paste time (this is the ODQ-5 delta dataset).

### 9.4 Mode 2 — RPA (Playwright)

**Decision gate DG-1 (week 1 of Phase 2, Aryia + Mayfair IT):** confirm ICON and Powerhub are browser-accessible (both believed web; if ICON is thick-client, RPA for ICON moves to a Windows-automation spike and paste-assist remains its mode — nothing else changes). **Credentials:** dedicated service accounts `svc-pacha-icon`, `svc-pacha-edms` requested from Mayfair IT with officer-equivalent permissions minus approval rights — RPA **never** runs under a person's login (audit separation; this request goes in writing this week, lead time risk). Secrets in AWS Secrets Manager; sessions isolated per run. **Deployment topology per ED-3a (v1.1):** the RPA worker is an outbound-only container ("runner") that pulls jobs from the queue API, pushes evidence to S3, and heartbeats — no inbound ports. Host = Mayfair-provided VM/mini-PC if DG-3 (open item 12) returns LAN-only; identical container on Fargate behind NAT with static EIPs if internet-reachable with allowlisting. Build to the container contract; the host decision changes nothing else.

**Click-path spec format** — versioned YAML in the pack repo, one file per operation:

yaml

```yaml
operation: icon.claim_register
version: 1.0.0
preconditions: [{assert: logged_in}, {assert: module, equals: Claims}]
steps:
  - {id: s1, action: click,  selector: 'role=menuitem[name="Claim Registration"]'}
  - {id: s2, action: fill,   selector: '#policyNo', value: '{policy.number}'}
  - {id: s3, action: select, selector: '#classCode', value_map: pack.icon_class_codes}
  - {id: s4, action: select, selector: '#lossCause',
     value: '{rule:R-05 ? "OD Write Off" : "Own Damage Accidental"}'}
  - {id: s5, action: fill,   selector: '#lossDesc', value: '{generated.loss_description}'}
  # ... loss date/time, intimation date/time, dupe-check button + assert-no-dupe,
  #     save, risk-select by reg, register — full sequence from as-is 3.2
readback:
  - {capture: claim_number, selector: '#claimNoResult', into: external.icon.claim_no,   # canonical claim_fields path (v1.1)
     assert_format: 'icon_claim_no_regex'}
failure_policy: screenshot_always, halt_on_selector_miss, no_guessing
```

Runtime rules (hard): screenshot before/after every step → S3 → `projections.evidence` (Pace-style UI-level audit); **selector miss = halt + `EXCEPTION{type: ui_drift}` + auto-fallback of that operation to paste_assist mode** — the agent never hunts for an alternative element; timeouts per step (default 20s; EDMS ops 90s given observed 6-min document handling, W-04 — upload steps get 8-min ceiling + progress polling); known-failure handlers coded explicitly: `edms.attach_and_tag` duplicate-filename rejection (W-11) → deterministic rename `{original}__{claim_id_suffix}{n}` and retry once; EDMS slow reflection → poll search every 30s, 10-min ceiling, then EXCEPTION.

Click-paths are **recorded at the embed** (Playwright codegen session with Gilbert on each form, then hand-hardened) — this is a scheduled discovery task, 1 day per system, prerequisite for any RPA op leaving L1.

### 9.5 Reconciliation (non-negotiable invariant)

Every completed write is followed by `readback(op, keys)` — re-read the entered values from the target (RPA: navigate to the record, scrape the same fields; paste-assist: officer attestation stands in, plus weekly sampled human readback of 10% of paste-assist ops as a check-the-checker — surfaced as review item type `PASTE_READBACK_CHECK`, v1.1). Compare against `payload` snapshot: mismatch ⇒ status `diverged`, `projection.diverged` event, `EXCEPTION{type: divergence}` review item showing both values + evidence screenshots. **The platform never auto-corrects a divergence** — a human decides which side is wrong (target system edited out-of-band is a real possibility). Standing nightly job re-reads `external_refs`-bearing claims' key financial fields (reserve total, status) from ICON when RPA read exists → drift report. Dashboard tile: divergence rate (target: 0; any non-zero pages the team).

### 9.6 Capabilities & sequencing

Per-operation capabilities `project.<operation>`: paste_assist mode ≡ L1 by construction; RPA introduction: L2 (officer watches a live run via streamed screenshots, confirms) → L3 (auto + 20% sampling) → L4, standard promotion policy, **except** `icon.payment_voucher` + both EDMS payment workflows: ceiling L2 until PRD-12's gate opens (≥1 quarter at L3+ elsewhere + CM sign-off), per the money-last constitution. Registration order of go-live: `edms.claims_workflow` first (biggest re-key block, W-01), then `icon.claim_register` + `reserve_*`, then `attach_and_tag`.

### 9.7 Acceptance

(1) Paste-assist: full registration via field strip against ICON staging/training environment ❓(confirm existence of ICON test instance with Mayfair IT — if none, first 25 RPA runs execute on real claims at L2 with officer watching, which the ladder already requires); claim number readback lands in `external_refs`; (2) RPA `edms.claims_workflow` replay on 5 claims: 14 fields entered, screenshots complete, readback matches, zero divergence; (3) inject a changed selector → halt, exception, auto-fallback to paste-assist, no partial writes beyond last screenshotted step; (4) duplicate-filename fixture → rename-retry succeeds once, second collision → exception; (5) kill worker mid-operation → resume produces no double entry (idempotency proof against target system); (6) deliberately edit a value in EDMS after projection → nightly drift job flags it within 24h.