## PRD-00 — Canonical Claim Object & Event Spine (build spec)

> **v1.1** — incorporates the CTO decision round of 2026-07. Where this document conflicts with Section 0 (Shared Engineering Decisions) or Section 0.5 (Shared Agent Runtime), those files win. Anything underdetermined: follow ED-11 (never decide locally — route to the open-items register).

### 0.1 Purpose

One claim = one record. Everything downstream reads and writes this object; ICON/EDMS are projections. This PRD delivers: the claim data model with field-level provenance, the append-only event spine, the claim lifecycle state machine, SLA clocks, the audit ledger, and PII handling.

### 0.2 Data model (DDL-level)

sql

```sql
-- The claim root. Thin on purpose: real data lives in claim_fields.
CREATE TABLE claims (
  id            TEXT PRIMARY KEY,            -- ULID
  lob           TEXT NOT NULL,               -- 'motor' | future pack ids
  pack_version  TEXT NOT NULL,               -- pinned at creation, e.g. 'motor@1.3.0'
  status        TEXT NOT NULL,               -- FSM state, see 0.4
  substatus     TEXT,                        -- e.g. 'EX_GRATIA_REVIEW' under DECLINED (see 0.4)
  external_refs JSONB NOT NULL DEFAULT '{}', -- DENORMALISED READ CACHE ONLY — see note below
  dek_wrapped   BYTEA,                       -- per-claim data-encryption key, KMS-wrapped (ED-6a)
  assigned_to   TEXT,                        -- owning officer user id (assignment model, PRD-05 §5.8)
  created_at    TIMESTAMPTZ NOT NULL,
  updated_at    TIMESTAMPTZ NOT NULL,
  closed_at     TIMESTAMPTZ
);

-- Field store: append-only versions. Current value = highest version not superseded.
CREATE TABLE claim_fields (
  id                 TEXT PRIMARY KEY,       -- ULID
  claim_id           TEXT NOT NULL REFERENCES claims(id),
  path               TEXT NOT NULL,          -- dot notation: 'vehicle.reg', 'loss.date',
                                             -- 'assessment.agreed_quote', 'reserve.total'
  value              JSONB NOT NULL,         -- typed per field dictionary
  value_type         TEXT NOT NULL,          -- 'string'|'money'|'date'|'datetime'|'bool'|'enum'|'object'
  source_type        TEXT NOT NULL,          -- 'extraction'|'calc'|'rule'|'human'|'system'|'projection_readback'
  source_ref         JSONB,                  -- extraction: {document_id, page, bbox:[x0,y0,x1,y1], anchor_text}
                                             -- calc: {calc_id, calc_run_id}; human: {user_id, review_item_id}
  confidence         NUMERIC(4,3),           -- null for human/system sources
  verification_state TEXT NOT NULL,          -- 'extracted'|'human_verified'|'system_confirmed'
  pii_class          TEXT NOT NULL DEFAULT 'none',  -- 'none'|'personal-low'|'personal'|'sensitive'
  value_search       TEXT,                    -- blind index: HMAC-SHA256(normalised value) under the
                                              -- KMS index key; populated only for national ID, KRA PIN,
                                              -- DL number, phone, bank account (ED-6a). Encrypted PII
                                              -- values are equality-searchable ONLY via this column.
  version            INT NOT NULL,
  superseded_by      TEXT REFERENCES claim_fields(id),
  created_by         TEXT NOT NULL,          -- 'agent:intake'|'user:<ulid>'|'system'
  created_at         TIMESTAMPTZ NOT NULL,
  UNIQUE (claim_id, path, version)
);
CREATE INDEX ix_fields_current ON claim_fields (claim_id, path) WHERE superseded_by IS NULL;

CREATE TABLE documents (
  id            TEXT PRIMARY KEY,
  claim_id      TEXT NOT NULL REFERENCES claims(id),
  doc_type      TEXT,                        -- from pack taxonomy; null until classified
  status        TEXT NOT NULL,               -- 'received'|'classified'|'extracted'|'verified'|'rejected'
  filename      TEXT NOT NULL,
  mime          TEXT NOT NULL,
  s3_key        TEXT NOT NULL,               -- immutable original
  sha256        TEXT NOT NULL,               -- dedupe + tamper evidence
  page_count    INT,
  source        JSONB NOT NULL,              -- {"channel":"email","message_id":"...","sender":"..."}
  received_at   TIMESTAMPTZ NOT NULL
);

CREATE TABLE communications (                -- every email in/out, later SMS/voice
  id TEXT PRIMARY KEY, claim_id TEXT REFERENCES claims(id),
  direction TEXT NOT NULL, channel TEXT NOT NULL DEFAULT 'email',
  graph_message_id TEXT UNIQUE, thread_id TEXT,
  from_addr TEXT, to_addrs JSONB, subject TEXT,
  body_s3_key TEXT NOT NULL, sent_by TEXT,   -- 'agent:chase'|'user:<id>'
  occurred_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE parties (
  id TEXT PRIMARY KEY, claim_id TEXT NOT NULL REFERENCES claims(id),
  role TEXT NOT NULL,                        -- 'insured'|'broker'|'agent'|'driver'|'garage'|
                                             -- 'assessor'|'supplier'|'third_party'|'bank'|'salvage_yard'
  name TEXT, email TEXT, phone TEXT, meta JSONB DEFAULT '{}'
);
```

Field dictionary: the platform ships a **core dictionary** (path → type, PII class, validation) covering `policy.*`, `loss.*`, `intimation.*`, `parties.*`, `reserve.*`, `settlement.*`; packs register extensions (`vehicle.*`, `assessment.*`, `salvage.*` from the Motor Pack). Writes to unregistered paths are rejected (`422 FIELD_NOT_IN_DICTIONARY`) — this is what keeps LOB packs honest.

**Write rule (hard invariant):** nothing updates a field in place. A new version row is written, prior version's `superseded_by` set, and a `field.updated` event emitted — one transaction. Human values always supersede agent values; an agent may never supersede a `human_verified` field (attempt → `409 HUMAN_OVERRIDE_PROTECTED` + review item).

**External-system references (canonical location):** the core dictionary registers `external.icon.claim_no`, `external.icon.salvage_no`, `external.edms.*` as first-class field paths (source_type `projection_readback`, full provenance). **`claim_fields` is canonical** for these values; `claims.external_refs` is a denormalised read cache for joins/search only, updated by a single dedicated consumer subscribed to `field.updated` on `external.*` paths — nothing else writes it. Audit and grading always read the field, never the cache.

**Write concurrency (binding repository behaviour):** field writes are optimistic — on unique-violation (two writers raced the same `(claim_id, path, version)`), re-read the current version, reapply, max 3 retries, then error. `PATCH /claims/{id}/fields` batches are **atomic**: one transaction holding a per-claim advisory transaction lock (`pg_advisory_xact_lock(hashtext(claim_id))`) — all-or-nothing, serialized per claim (trivial cost at design volume).

### 0.3 Event spine

sql

```sql
CREATE TABLE events (
  id             TEXT PRIMARY KEY,           -- ULID = identity
  seq            BIGSERIAL,                  -- transport order for external replay ONLY (see below)
  claim_id       TEXT,                       -- nullable for platform events
  type           TEXT NOT NULL,              -- catalog below
  payload        JSONB NOT NULL,
  actor          TEXT NOT NULL,
  correlation_id TEXT,                       -- ties multi-step agent runs together
  occurred_at    TIMESTAMPTZ NOT NULL
);
CREATE TABLE event_deliveries (              -- at-least-once + idempotent consumers
  event_id TEXT REFERENCES events(id), consumer TEXT,
  status TEXT NOT NULL, attempts INT DEFAULT 0, last_error TEXT,
  PRIMARY KEY (event_id, consumer)
);
```

Mechanics: **transactional outbox**. Event rows are written in the same transaction as the state change; a Celery dispatcher (poll every 2s, `FOR UPDATE SKIP LOCKED`) fans out to registered consumers; consumers are idempotent (dedupe on `(event_id, consumer)`); retry with exponential backoff, max 8 attempts, then dead-letter + ops alert. No Kafka, no Temporal — Postgres is the bus at this volume, and durability comes free.

**Replay cursor:** the external replay API (`GET /events`) orders by `seq` and serves only rows older than a **5-second watermark** (closes the late-commit visibility gap under concurrent writers without fencing machinery). ULIDs remain the identity; `seq` is transport order only. Internal dispatch is unaffected (single outbox reader). The `after_ulid` parameter in §0.7 is replaced by `after_seq`.

**Event catalog v1** (consumers subscribe by type; payloads schema'd in code):

|Domain|Events|
|---|---|
|Claim|`claim.created`, `claim.status_changed`, `claim.reopened`|
|Documents|`document.received`, `document.classified`, `document.extracted`, `document.rejected`|
|Fields|`field.updated`, `field.verified`|
|COP runtime|`rule.evaluated`, `calc.executed`, `template.rendered`|
|Review|`review.created`, `review.resolved`|
|SLA|`sla.started`, `sla.warned`, `sla.breached`, `sla.stopped`|
|Chase|`chase.item_requested`, `chase.reminder_sent`, `chase.item_received`, `chase.complete`|
|Projection|`projection.requested`, `projection.completed`, `projection.failed`, `projection.diverged`|
|Autonomy/eval|`grader.passed`, `grader.failed`, `autonomy.promoted`, `autonomy.demoted`|
|Comms|`email.received`, `email.sent`|

Long-running waits (document chase, repair, bid windows) are **not** blocked threads: they are SLA clock rows + Beat ticks that emit events. Nothing in the system sleeps.

### 0.4 Claim lifecycle FSM

**Complete state set (v1.1 — this list is exhaustive; agents implement exactly these):**
`INTIMATED, TRIAGED, AWAITING_DOCS, IN_ASSESSMENT, REPORT_RECEIVED, REGISTERED, RESERVED, PACK_READY, IN_APPROVAL, APPROVED, IN_REPAIR, REINSPECTION, RELEASED, WRITE_OFF, SALVAGE_BIDDING, CLIENT_ELECTION, SURRENDER_CHECKLIST, RETAINED, SETTLEMENT, SETTLED, CLOSED, DECLINED, WITHDRAWN, VOID`

Primary transitions (guards in parentheses; enforced in one place — `ClaimStateMachine.transition()` — everything else calls it):

```
INTIMATED → TRIAGED (coverage+excess evaluated)
TRIAGED → AWAITING_DOCS
AWAITING_DOCS → IN_ASSESSMENT (estimate received)
IN_ASSESSMENT → REPORT_RECEIVED (assessor report parsed)
REPORT_RECEIVED → WRITE_OFF (R-05 true) | REGISTERED (external.icon.claim_no captured)
REGISTERED → RESERVED (C-02/C-03 EXECUTED LOCALLY with verified inputs —
                        projection is a parallel tracker, NOT a guard; see PRD-08 §8.2)
RESERVED → PACK_READY (manifest complete + note drafted)
PACK_READY → IN_APPROVAL (officer signed note)
IN_APPROVAL → APPROVED | (reject → PACK_READY with structured reasons)
APPROVED → IN_REPAIR → REINSPECTION (R-08 routing) → RELEASED
WRITE_OFF → SALVAGE_BIDDING → CLIENT_ELECTION → SURRENDER_CHECKLIST | RETAINED
RELEASED | SURRENDER_CHECKLIST(complete, R-13/R-14 gate) | RETAINED → SETTLEMENT
SETTLEMENT → SETTLED → CLOSED
```

**Decline (action, not a single transition):** `decline(reason)` with reason enum `{below_excess, out_of_cover, fraud, non_disclosure, late_intimation, other}`.
- From `TRIAGED`: standard path (R-02 etc.), officer releases per PRD-05.
- From `{AWAITING_DOCS, IN_ASSESSMENT, REPORT_RECEIVED, REGISTERED, RESERVED, PACK_READY}`: permitted, but **requires a `claims_manager` approval item** before the transition commits.
- `DECLINED` is terminal, reopenable. `EX_GRATIA_REVIEW` is a **substatus of DECLINED** plus a review item — not a primary state.

**Withdrawal / void:**
- `WITHDRAWN`: reachable from any open state before `SETTLEMENT`; terminal, reopenable (insured abandons the claim).
- `VOID`: created-in-error only; permitted **only pre-REGISTERED** (no ICON record exists). After registration, use `WITHDRAWN`.
- All three terminals (`DECLINED, WITHDRAWN, VOID`) plus `SETTLED, CLOSED` suppress all chase and SLA activity (PRD-06 §6.4 suppression list is extended accordingly).

Parallel trackers (independent of FSM, shown on the status rail): document checklist state, projection state per external system, SLA clocks, review-queue items open. `blocked_reasons[]` on the claim surfaces hard gates (e.g., `R-13: logbook not held`).

### 0.5 SLA clock engine

`sla_definitions` ship in the pack: `{id, name, start_event, stop_event, warn_after, breach_after, escalate_to_role, calendar}`. **`calendar ∈ {24x7, send_window, business}`** — new v1.1 attribute, binding:

| clock | calendar | values |
|---|---|---|
| acknowledge | **24x7** (confirmed intended; L3 ack runs round the clock and the trial metric counts it) | start `claim.created`, warn 30m, breach 2h |
| doc_item_age (per checklist item) | calendar days, but sends respect the AR-3a send window (08:00–18:00 EAT, Mon–Sat, KE public holidays excluded) | warn 3d, breach 7d |
| assessor_turnaround | **business** days | warn 3d, breach 5d |
| approval_dwell (per band) | **business** hours | warn 24h, breach 72h |
| repair_duration | calendar | warn 14d |
| bid_window | calendar (close is a published fixed timestamp) | fixed 4d |

The Kenya public-holiday calendar ships in the pack (`sla/holidays.yaml`), annually maintained. Beat tick every 5 minutes evaluates open clocks → emits `sla.warned` / `sla.breached` → PRD-04 renders + escalates. Every clock row keeps started/stopped timestamps — **this table is the outcome-pricing baseline dataset**; it is never purged.

### 0.6 Audit ledger

Separate append-only table `audit_ledger(id, seq BIGSERIAL, occurred_at, actor, action, claim_id, object_ref, before_hash, after_hash, detail JSONB, row_hash)` where `row_hash = SHA256(prev.row_hash ‖ canonical_json(row))` — a hash chain making tampering evident. Written for: every field version, every FSM transition, every LLM call (model, prompt template id, token counts, redacted payload ref), every outbound email, every projection write, every autonomy change, every human review action. Nightly job re-verifies chain integrity and anchors the day's head hash into S3 Object-Lock (WORM). This is the DPA/IRA/ISO-27001 substrate and the anti-tamper answer that matches Mayfair's existing PDF-only posture.

**Serialization (binding):** all ledger appends flow through the event spine to **one dedicated Celery queue with `concurrency=1`**; that single consumer assigns `seq` from its own counter and computes the hash chain. No other code path writes `audit_ledger`. Gaps and commit-order races are structurally impossible.

**On nightly verification failure — audit-degraded mode:** (a) autonomy promotions frozen platform-wide; (b) every L3+ action is additionally dual-written directly to the S3 WORM anchor until the chain is repaired and re-verified; (c) ops paged; (d) incident review is mandatory before promotions unfreeze. **No automatic demotions** — the failure is in the ledger, not the agents.

### 0.7 API surface (internal REST, OpenAPI-generated)

`POST /claims` · `GET /claims/{id}` (hydrated object: current fields + docs + trackers) · `GET /claims/{id}/timeline` (events) · `PATCH /claims/{id}/fields` (batch versioned writes) · `POST /claims/{id}/transition` · `GET/POST /claims/{id}/documents` · `GET /claims?status=&lob=&sla_breached=` · `GET /events?after_seq=` (consumer replay; ordered by `seq`, 5s watermark per §0.3). AuthZ per PRD-04 roles on every route.

### 0.8 NFRs and acceptance

p95 hydrated claim read < 300ms; event publish→consume p95 < 5s; RPO 15min (PITR), RTO 4h; 99.5% availability during 07:00–19:00 EAT. **Acceptance scenarios:** (1) create claim, write 50 field versions incl. supersessions, hydrate returns exactly current values with full provenance; (2) attempt agent overwrite of human-verified field → 409 + review item; (3) kill worker mid-dispatch → no event lost, no duplicate side-effect (idempotency proven); (4) tamper a ledger row in staging → nightly verify fails loudly; (5) illegal FSM transition rejected with reason; (6) SLA breach fires within 5min of threshold.