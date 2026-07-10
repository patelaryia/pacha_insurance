## PRD-11 — Salvage Auction Platform (build spec)

> **v1.1** — incorporates the CTO decision round of 2026-07. Where this document conflicts with Section 0 (Shared Engineering Decisions) or Section 0.5 (Shared Agent Runtime), those files win. Anything underdetermined: follow ED-11 (never decide locally — route to the open-items register).

### 11.1 Purpose

Digitise the salvage disposal loop: ICON salvage registration → published lot → sealed bids from onboarded yards → committee award with counter-offers → client election and surrender gates → recovery captured to the savings ledger. Deletes the physical file (W-08) and the ≥4-day paper bid cycle, and creates the recovery dataset. This is the platform's only externally-facing surface — treat it as hostile territory.

### 11.2 Architecture & security posture

Same monolith, separate FastAPI router + separate React bundle served at `bids.<domain>` behind its own CloudFront distribution. **No SSO, no Mayfair identities**: bidders authenticate by magic link (single-use token, 15-min validity, bound to bidder email; session cookie 24h, httpOnly). **Auth surfaces (v1.1):** per-lot invitation links **plus** a bidder-initiated login page (email → magic link) showing only that bidder's invited open lots and own bid history — **nothing else exists between lots**. **Edge stack (v1.1):** AWS WAF (managed core rule set + known-bad-inputs) + rate rule 100 req/5min/IP in front of the portal CloudFront distribution, in addition to the per-bidder application rate limit below. **Pen test (v1.1):** external firm, before the first live lot; scope = OWASP ASVS Level 1 plus the §11.6(5) checks; pass bar = **zero open high/critical findings**. Hard isolation rules, enforced at the router layer and covered by tests: portal endpoints can only read a `lot_public` projection (whitelist: photos via 1h signed URLs, make/model/year, reg, damage summary, yard location, window times) — **no insured name, no claim id, no policy data ever crosses this boundary**; lots addressed by `public_ref` (ULID, unguessable), no enumeration endpoint; per-bidder rate limits (10 req/min), `X-Robots-Tag: noindex`, every bidder action ledgered. Reg plate stays visible — it's in the current bid letters and yards need it; strip everything else.

### 11.3 Data model

sql

```sql
CREATE TABLE bidders (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, emails JSONB NOT NULL, phone TEXT,
  kyc_status TEXT NOT NULL DEFAULT 'pending',   -- 'pending'|'verified'|'suspended'
                                                -- v1.1: verification is review item type KYC_VERIFY
                                                -- (officer reviews docs; resolution sets status)
  kyc_docs JSONB,                               -- cert of inc, KRA PIN, ID — reuse PRD-01 schemas
  bond JSONB,                                   -- {required: bool, amount, held: bool} ❓ODQ-8
  created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE salvage_lots (
  id TEXT PRIMARY KEY, public_ref TEXT UNIQUE NOT NULL, claim_id TEXT NOT NULL,
  status TEXT NOT NULL,   -- 'draft'|'published'|'closed'|'under_review'|'awarded'|'cancelled'
  icon_salvage_no TEXT,   -- readback from icon.salvage_register (PRD-09 op)
  description TEXT, yard_location TEXT, photo_doc_ids JSONB,
  reserve_estimate BIGINT,          -- assessor salvage_value; committee-only, never portal.
                                    -- NULLABLE (v1.1): assessor may give no salvage value — see S9
  window_opens_at TIMESTAMPTZ, window_closes_at TIMESTAMPTZ,
  awarded_bid_id TEXT, created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE bids (       -- v1.1: append-only, claim_fields pattern (unique constraint replaced)
  id TEXT PRIMARY KEY, lot_id TEXT NOT NULL, bidder_id TEXT NOT NULL,
  amount BIGINT NOT NULL, submitted_at TIMESTAMPTZ NOT NULL,
  status TEXT NOT NULL,   -- 'sealed'|'revealed'|'awarded'|'declined'|'countered'|'withdrawn'
  superseded_by TEXT REFERENCES bids(id),   -- amendment = new row superseding prior
  in_counter BOOLEAN NOT NULL DEFAULT false -- true only for rows created by the counter flow (S7)
);
CREATE UNIQUE INDEX ux_bids_live ON bids (lot_id, bidder_id) WHERE superseded_by IS NULL;
-- Amendment rules (v1.1, binding): amendments allowed ONLY while lot.status='published'.
-- After close, the only path is the counter flow: a countered bidder's accept/rebid creates
-- a new row flagged in_counter=true; non-countered bidders cannot move.
CREATE TABLE lot_messages (          -- committee ↔ bidder negotiation thread, post-close only
  id TEXT PRIMARY KEY, lot_id TEXT, bidder_id TEXT, direction TEXT,
  body TEXT, sent_by TEXT, occurred_at TIMESTAMPTZ
);
```

### 11.4 Flow (capability set `salvage.*`)

```
S1 register     on FSM→WRITE_OFF: project icon.salvage_register (SI, PAV, agreed
                salvage value, location — click-path per PRD-09 §9.4) → salvage
                number readback. Paste-assist until RPA lands.
S2 notify       client write-off letter T-12 ❓capture: 50% rule, yard storage,
                retain/surrender election + deadline. DRAFT_RELEASE at launch.
S3 election     client response → officer records election (console action).
                DEADLINE (v1.1): 14 days (pack config); on silence → escalation
                review item at T+14 — NEVER auto-surrender (legal posture).
                  retained  → C-07 retained variant; lot cancelled/never published;
                              skip to PRD-12 with DV = PAV/SI-capped − excess − salvage_value
                  surrendered → surrender checklist instantiated (PRD-06 §6.5:
                              logbook original, keys, Cert of Inc, KRA PIN,
                              + bank_discharge_letter when R-14 trips) AND auction proceeds
S4 publish      lot composed from claim (photos, damage summary auto-drafted from
                assessor report — MODEL_LIGHT, officer confirms); bid invitation
                T-02 rendered → emailed to all verified bidders with portal link.
                Window: opens on publish, closes +4 days (pack config, ≥ current
                practice floor). Beat job auto-closes at window_closes_at.
S5 sealed bids  bidders submit/amend until close. Sealing is enforced server-side:
                committee/officer endpoints return 403 on bid reads while
                status='published' — no UI-only hiding. Bidder sees only own bid.
S6 review       on close: reveal, rank desc, present committee console (S-3 variant):
                bid table, reserve_estimate, spread vs estimate. Committee =
                head_of_claims + md + gm; award requires 2-of-3 approvals
                (quorum ❓ODQ-8 confirm; default 2-of-3), each ledgered.
S7 counter      optional: committee counters top bidder(s) via lot_messages →
                portal + email notification; bidder accept/decline/rebid within
                48h; accept = new bid row at counter amount (in_counter=true).
                EXPIRY (v1.1): a Beat job closes counters at 48h → status
                'counter_expired', committee notified.
S8 award        winner notified (T-02b award letter ❓); payment collection is
                OFFLINE v1 — officer marks 'payment received' (attested, ledgered);
                collection/release-to-yard note gated on that mark.
S9 recovery     savings_ledger row: kind='salvage_recovery',
                baseline=reserve_estimate, achieved=awarded amount, evidence=
                {lot_id, bid_id, committee approvals}. Recovery-rate tile on S-4.
                NO-BASELINE CASE (v1.1): reserve_estimate NULL → write the row with
                baseline := awarded, saving = 0, evidence flag no_baseline: true —
                dataset completeness without fabricated savings.
S10 gate        settlement path continues only when surrender checklist complete
                (R-13/R-14 — FSM guard, already built in PRD-06/00).
```

No-bid outcome: window closes with zero bids → `EXCEPTION{type: no_bids}` → committee chooses republish (new window) or direct-negotiation (offline, officer records outcome). Do not auto-extend windows.

### 11.5 Capabilities

`salvage.register` (max L4, launch L1) · `salvage.publish` (max L3 — external publication, sampled forever) · `salvage.close_and_rank` (max L4 — deterministic clock + sort, fast-track L3) · `salvage.award` — **not a capability**; committee-only human action by construction, same class as approval authority · `salvage.recovery_ledger` (L4).

### 11.6 Acceptance

(1) Full simulation: 3 seeded verified bidders, publish → 3 bids (one amended), sealed-read attempt as CM before close → 403, auto-close on schedule, rank correct, 2-of-3 award, recovery row written; (2) counter-offer round trip through portal; (3) retained election → lot never published, C-07 retained figure matches hand calc; (4) settlement attempted with keys unattested → blocked with R-13 surfaced; (5) portal pen-check suite: lot enumeration attempt, expired magic link, cross-bidder bid read, insured-name grep across every portal response on a seeded lot (must be zero hits); (6) bidder onboarding: unverified bidder receives no invitations and cannot authenticate into an open lot.