## PRD-11 — Salvage Auction Provider Integration (build spec)

> **v1.1, architecture freeze 2026-07-24** — supersedes the custom
> bidder-facing portal with ADR-004's established auction-provider strategy.
> Where this document conflicts with Section 0 or Section 0.5, those files win.
> Anything underdetermined: follow ED-11.

### 11.1 Purpose and boundary

Pacha governs write-off eligibility, retain/surrender election, lot readiness,
the approved minimal lot export, committee award, verified result import,
settlement gates and recovery measurement.

An established auction provider owns bidder authentication, bidder KYC, bid
submission and sealing, bidder-facing security, auction hosting, counter-offer
transport, bidder support and external penetration testing of its service.
Pacha exposes no bidder portal, magic links, bidder sessions, bid-submission API
or bidder-support surface.

The first pilot may use controlled CSV/PDF export and CSV/PDF or manually
attested results. An API is optional. No provider is selected without an RFI,
commercial/security due diligence, data-processing review and DPIA approval.

### 11.2 Provider-boundary security

Only an approved `lot_export` projection crosses the provider boundary:

`photos, vehicle.make, vehicle.model, vehicle.year, vehicle.reg,
damage_summary, yard_location, window_instructions`.

Claim id, insured identity, policy data, bank data, contact data, internal
reserves, authority commentary and all other claim facts are prohibited.
Outbound artifacts are generated from the whitelist, hash-pinned, encrypted in
transit, access-logged and scanned for insured/policy identifiers before
release. A failed scan refuses export. Provider result files are immutable
artifacts, quarantined until GuardDuty Malware Protection for S3 reports them
safe, schema-validated and retained with their attestation and hash.

An API integration, if later selected, uses least-privilege service identity,
stable request idempotency, signed responses where available and the same
export/import validation. It cannot broaden the whitelist.

### 11.3 Data model

```sql
CREATE TABLE salvage_lots (
  id TEXT PRIMARY KEY, claim_id TEXT NOT NULL,
  status TEXT NOT NULL,        -- 'draft'|'ready'|'exported'|'results_received'|
                               -- 'under_review'|'awarded'|'cancelled'|'exception'
  provider_ref TEXT,           -- opaque provider reference after verified import
  description TEXT, yard_location TEXT, photo_doc_ids JSONB,
  reserve_estimate BIGINT,     -- nullable assessor salvage_value; Pacha-only
  window_opens_at TIMESTAMPTZ, window_closes_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE auction_exports (
  id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES salvage_lots(id),
  format TEXT NOT NULL,        -- 'csv'|'pdf'|'api'
  artifact_ref TEXT NOT NULL, payload_hash TEXT NOT NULL,
  whitelist_version TEXT NOT NULL,
  status TEXT NOT NULL,        -- 'prepared'|'approved'|'released'|'rejected'
  approved_by TEXT, released_at TIMESTAMPTZ,
  UNIQUE (lot_id, payload_hash)
);

CREATE TABLE auction_result_imports (
  id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES salvage_lots(id),
  source_kind TEXT NOT NULL,   -- 'csv'|'pdf'|'manual_attestation'|'api'
  artifact_ref TEXT, source_hash TEXT NOT NULL,
  provider_ref TEXT,
  status TEXT NOT NULL,        -- 'quarantined'|'pending_verification'|'verified'|
                               -- 'exception'
  attested_by TEXT, attested_at TIMESTAMPTZ,
  imported_at TIMESTAMPTZ NOT NULL,
  UNIQUE (lot_id, source_hash)
);

CREATE TABLE auction_result_rows (
  id TEXT PRIMARY KEY,
  import_id TEXT NOT NULL REFERENCES auction_result_imports(id),
  provider_bid_ref TEXT,       -- opaque; required when supplied by provider
  bidder_alias TEXT,           -- provider-visible alias only; no Pacha KYC profile
  amount BIGINT NOT NULL,      -- KES cents
  rank INT,
  status TEXT NOT NULL,        -- 'offered'|'withdrawn'|'countered'|'final'
  source_row_ref TEXT NOT NULL
);

CREATE TABLE auction_awards (
  id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES salvage_lots(id),
  result_row_id TEXT NOT NULL REFERENCES auction_result_rows(id),
  status TEXT NOT NULL,        -- 'proposed'|'approved'|'rejected'
  decision_evidence JSONB NOT NULL,
  decided_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX ux_auction_award_approved
  ON auction_awards (lot_id) WHERE status='approved';
```

Result rows are append-only. Import correction is a new immutable import; Pacha
never edits provider evidence in place. An ambiguous duplicate provider
reference, amount, lot match or result version creates
`EXCEPTION{auction_result_ambiguous}` and blocks award.

### 11.4 Flow

```text
S1 register     on FSM→WRITE_OFF: project icon.salvage_register through PRD-09;
                keep paste-assist until an executor operation is accepted.
S2 notify       client write-off letter T-12 (pending capture); human release.
S3 election     officer records retain/surrender. Deadline remains 14 days in
                pack config; silence creates review and NEVER auto-surrenders.
                retained → C-07 retained variant; no auction export.
                surrendered → blocking PRD-06 surrender checklist + lot draft.
S4 readiness    require verified election, approved lot fields/photos, yard,
                window instructions and provider selection. Missing provider or
                format → blocked_on_inputs. Internal reserve never enters export.
S5 export       generate CSV/PDF from lot_export only; run identifier scan;
                officer approves; release with hash and attestation. API may
                replace transport later without changing payload authority.
S6 provider     provider runs KYC, sealed bids, counter offers and bidder support.
                Pacha records only deadline/operational status; it never receives
                live sealed bids or exposes a bidder surface.
S7 import       receive provider results, quarantine/scan, hash, schema-validate,
                match exact lot/provider ref, normalise money to KES cents and
                require a provider report or human attestation.
S8 verify       compare result totals/ranks and provider evidence. Missing,
                malformed, duplicate or ambiguous data → EXCEPTION; never select.
S9 award        committee reviews verified final results and decides. Award is
                not a capability and never automatic. Quorum remains
                blocked_on_inputs until Mayfair confirms it; no default quorum.
                Provider owns winner/counter-offer transport; Pacha exports or
                records the approved instruction and imports final confirmation.
S10 recovery    write savings_ledger salvage_recovery from the verified final
                result and committee award. If reserve_estimate is NULL, use
                baseline=awarded, saving=0, evidence.no_baseline=true.
S11 gate        settlement proceeds only when the surrender checklist and
                R-13/R-14 are complete.
```

No-result/no-bid outcome creates `EXCEPTION{no_bids}`. The committee may approve
a new provider window or an offline direct-negotiation record; Pacha never
auto-extends a provider auction.

### 11.5 EXCEPTION subtype contracts

Both cases use PRD-04's existing `EXCEPTION` review-item type; they do not add a
review-item enum member.

**`auction_result_ambiguous` v1:** (1) produced by
`auction.result_import_ambiguous` with `lot_id`, `import_id`, issue codes and
opaque source-row refs; (2) workspace shows the immutable provider artifact,
normalised candidate rows, validation differences and lot export side by side;
(3) actions are Reject import, Supersede with a new import, or Confirm exact
row-to-lot mapping. No action edits provider evidence or selects an award; (4)
resolution payload schema:

```json
{
  "$id": "pacha://review/exception/auction_result_ambiguous/v1",
  "type": "object",
  "additionalProperties": false,
  "required": ["action", "reason", "replacement_import_id"],
  "properties": {
    "action": {"enum": ["reject", "supersede", "confirm_mapping"]},
    "reason": {"type": "string", "minLength": 1},
    "replacement_import_id": {"type": ["string", "null"]},
    "confirmed_result_row_id": {"type": ["string", "null"]}
  }
}
```

`confirm_mapping` requires `confirmed_result_row_id`; `supersede` requires
`replacement_import_id`; other combinations fail validation.

**`no_bids` v1:** (1) produced by `auction.results_no_bids` only from a verified
provider result/attestation; (2) workspace shows the lot export, provider
attestation, window and verification evidence; (3) actions are Close without
award, Approve new provider window, or Record committee-approved offline
negotiation. None may fabricate a bid or automatically extend the window; (4)
resolution payload schema:

```json
{
  "$id": "pacha://review/exception/no_bids/v1",
  "type": "object",
  "additionalProperties": false,
  "required": ["action", "reason"],
  "properties": {
    "action": {"enum": ["close", "new_window", "offline_negotiation"]},
    "reason": {"type": "string", "minLength": 1},
    "approved_instruction_ref": {"type": ["string", "null"]}
  }
}
```

`new_window` and `offline_negotiation` require an opaque
`approved_instruction_ref`; no bidder or claim PII is carried in the resolution
payload.

### 11.6 Capabilities

`salvage.register` (max L3, launch L1) · `salvage.lot_prepare` (max L3,
launch L1) · `salvage.export` (max L2, human release permanently) ·
`salvage.result_import` (max L3, launch L1) · `salvage.result_verify` (max L3,
launch L1) · `salvage.recovery_ledger` (max L3).

`salvage.award` is **not a capability**. It is a committee-only human authority
decision. Provider selection and provider communication do not weaken Pacha's
financial/autonomy ceilings.

### 11.7 Acceptance

1. A ready surrendered lot produces a CSV and PDF containing exactly the
   `lot_export` whitelist; insured name, claim id, policy and bank-data scans
   return zero hits.
2. Missing provider, approval, whitelist field, safe malware result or
   attestation refuses export/import visibly.
3. The same export/import hash is idempotent; a changed file creates a new
   immutable version.
4. A malformed amount, duplicate lot/provider match, inconsistent rank or
   conflicting result becomes `auction_result_ambiguous`; no award is proposed.
5. Verified results support a committee award with complete evidence; award
   cannot be executed through `execute_or_stage` or any autonomy level.
6. Retained election creates no provider export and the C-07 retained figure
   matches the hand calculation.
7. Settlement with unattested keys/logbook is blocked by R-13/R-14.
8. Recovery rows reconcile to the verified final result, including the
   `no_baseline` case.
9. No Pacha route, bundle or schema implements bidder login, bidder KYC, bid
   submission/sealing, counter-offer transport or bidder sessions.
