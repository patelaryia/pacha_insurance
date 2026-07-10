## PRD-06 — Document Chase Agent (build spec)

> **v1.1** — incorporates the CTO decision round of 2026-07. Where this document conflicts with Section 0 (Shared Engineering Decisions) or Section 0.5 (Shared Agent Runtime), those files win. Anything underdetermined: follow ED-11 (never decide locally — route to the open-items register).

### 6.1 Purpose

Instrumented per-item document collection with automated reminders and hard-gate enforcement. Reused verbatim by salvage surrender (R-13/R-14) and any future LOB pack.

### 6.2 Data model

sql

```sql
CREATE TABLE chase_checklists (
  id TEXT PRIMARY KEY, claim_id TEXT NOT NULL, purpose TEXT NOT NULL,   -- 'claim_docs'|'surrender'
  status TEXT NOT NULL,                       -- 'open'|'complete'|'cancelled'
  blocking BOOLEAN NOT NULL DEFAULT false,    -- surrender = true → feeds blocked_reasons[]
  created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE chase_items (
  id TEXT PRIMARY KEY, checklist_id TEXT NOT NULL,
  item_id TEXT NOT NULL,                      -- FK to the pack checklist-item registry
                                              -- (packs/*/checklists/items.yaml, v1.1): every item is a
                                              -- registered entity {id, kind: document|physical|field_request,
                                              --  doc_type?, target_path?, physical: bool}.
                                              -- 'logbook_original'/'keys_physical' are kind=physical;
                                              -- 'incident_description' is kind=field_request → loss.narrative.
                                              -- Checklist instantiation VALIDATES against this registry —
                                              -- the 422 FIELD_NOT_IN_DICTIONARY path is closed by design.
  state TEXT NOT NULL,                        -- 'pending'|'requested'|'received'|'verified'|
                                              -- 'rejected'|'waived'
  physical BOOLEAN NOT NULL DEFAULT false,    -- keys/original logbook: receipt is human-attested
  requested_at TIMESTAMPTZ, received_at TIMESTAMPTZ, verified_at TIMESTAMPTZ,
  waived_by TEXT, waiver_reason TEXT,
  reminder_count INT NOT NULL DEFAULT 0, next_reminder_at TIMESTAMPTZ,
  document_id TEXT REFERENCES documents(id), reject_reason TEXT
);
```

Every state change emits `chase.item_*` events + timestamps — **this table is the cycle-time evidence base** (P-6); rows are never deleted.

### 6.3 Inbound matching

On `document.classified` for a claim with an open checklist: exact `doc_type` match to an outstanding item → `received`; on `document.extracted` passing validators → `verified` (else `rejected` with machine reason: illegible / wrong vehicle via CC-1 / expired / wrong document). A classified doc matching **no** outstanding item attaches to the claim with timeline note (never discarded). Rejection auto-composes a re-request naming the specific defect ("the logbook photo is illegible on the owner section — please resend") — capability `chase.rerequest`, launch L1.

### 6.4 Reminder engine

Beat tick (15 min) selects items where `next_reminder_at ≤ now`. Cadence (pack config): T+3d, T+7d, T+12d, then every 7d, **cap 6 reminders** → escalation item to officer (`EXCEPTION{type: chase_exhausted}`). Recipient ladder: requester party first (broker if broker-intimated — matches current practice), cc insured from reminder 2, officer alert at breach (SLA `doc_item_age`, PRD-00). Templates: `T-06r` reminder (lists **only outstanding** items, ✓ for received, per-item age) — tone variants `broker|client` selected by recipient role. Sends go through AR-3 at capability `chase.reminder` (L1 two weeks → L3 per fast-track policy: ≥25 clean, ≥96%).

Suppression rules (hard): FSM ∈ {DECLINED, **WITHDRAWN, VOID**, SETTLED, CLOSED} → checklist auto-cancelled (v1.1: terminal set extended per PRD-00 §0.4); officer can pause per-item (`snooze_until`); **deferral is per checklist (v1.1):** any inbound reply on the claim thread within 24h defers **all of that checklist's** reminders 48h — a human just engaged; item-granular deferral risks nagging someone mid-conversation and buys nothing. All reminder sends respect the AR-3a send window.

### 6.5 Hard-gate mode (surrender checklist — consumed by PRD-11/12)

`purpose='surrender', blocking=true`: items {logbook_original (physical), keys_physical (physical), cert_of_incorporation?, kra_pin_cert, bank_discharge_letter (auto-added when `logbook.bank_interest.present` — R-14)}. Physical items are human-attested: officer marks received in console (S-2 Documents tab), attestation ledgered. Checklist incomplete ⇒ `claims.blocked_reasons` includes `R-13`/`R-14` ⇒ PRD-12 settlement structurally cannot start (R-13 is a `block`-verb rule; the FSM guard on `→ SETTLEMENT` checks it). Waivers on blocking items require `claims_manager` role, reason mandatory, ledgered.

### 6.6 Analytics

Materialized views: per-doc-type median request→verified; per-broker responsiveness (median first-response, reminder count to completion) — the "broker league table"; chase-attributable cycle time per claim. Exposed via PRD-04 S-4 tiles + CSV.

### 6.7 Acceptance

(1) Replay corpus claim: 7-item checklist instantiated, 3 docs arrive across 3 emails → matched/verified, reminder at T+3d lists only the outstanding 4; (2) illegible logbook → rejected + defect-specific re-request drafted; (3) claim declined mid-chase → zero further reminders (assert on outbound log); (4) surrender checklist with bank interest → discharge-letter item auto-present; settlement transition attempted → blocked with `R-13/R-14` reasons surfaced; (5) reminder cap → escalation item; (6) analytics view returns non-null medians on seeded history.