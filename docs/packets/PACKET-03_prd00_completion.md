# PACKET-03 — PRD-00 completion: event dispatch, audit ledger, SLA engine, PII encryption, remaining API

> **Status:** issued · **Builder:** Codex per `AGENTS.md` · **Reviewer:** CTO per `CLAUDE.md`
> **Source spec:** `docs/PRD-00_Canonical_Claim_Object_and_Event_Spine_v1.1.md`
> §0.3 (dispatch/replay), §0.5, §0.6, §0.7 (remaining routes), §0.8 scenarios (3), (4), (6);
> Section 0 ED-6a (PII mechanics), ED-9 (retention notes). Precedence: Section 0 → PRD-00 → this packet.
> **Depends on:** PACKET-01 + PACKET-02 (both in `claim_core`).
> **Acceptance tests:** `tests/acceptance/test_packet_03_prd00_completion.py` — protected, failing by design.
> This packet **completes PRD-00** except: AuthZ (PRD-04), reopen (PRD-05, register #25),
> real S3/KMS backends + partitioning + NFR measurement (infra packet), pack-file loading (PRD-13).

## 1. Scope

**In:**
1. **Outbox dispatcher** — poll → fan-out to registered consumers, `event_deliveries`
   idempotency, retry w/ backoff, dead-letter; Celery wiring as thin config.
2. **Replay API** — `GET /events?after_seq=` with the 5-second watermark.
3. **`external_refs` cache consumer** — the single writer of `claims.external_refs`.
4. **Audit ledger** — §0.6 DDL verbatim, hash chain, single-writer consumer, chain
   verification job, WORM anchor via storage interface, audit-degraded mode.
5. **SLA clock engine** — definitions as data, clock rows (never purged), calendars,
   5-minute evaluation tick, `sla.started/warned/breached/stopped` events.
6. **PII envelope encryption (ED-6a)** — per-claim DEK, AES-256-GCM, pluggable key
   provider, `value_search` blind index, decrypt access-logging, dictionary additions.
7. **Remaining API** — `GET/POST /claims/{id}/documents`, `GET /claims?status=&lob=&sla_breached=`.
8. **CI upgrade** — PostgreSQL 16 + Redis 7 services; pytest runs against PG in CI
   (closes the CI half of register #21). *(CTO ships this part in the packet commit —
   builder does not touch `.github/`.)*

**Out:** AuthZ/RBAC (PRD-04); reopen (PRD-05); real AWS backends (S3 Object-Lock,
KMS), `pg_partman` partitioning, NFR load measurement (infra packet); chase/doc-item
clock *instantiation* (PRD-06 emits those start events); LLM-call/email/projection
ledger actions (their PRDs emit the events — the ledger consumer already maps them).

## 2. Binding spec quotes (implement verbatim)

PRD-00 §0.3, dispatch mechanics:

> "Mechanics: **transactional outbox**. Event rows are written in the same transaction
> as the state change; a Celery dispatcher (poll every 2s, `FOR UPDATE SKIP LOCKED`)
> fans out to registered consumers; consumers are idempotent (dedupe on
> `(event_id, consumer)`); retry with exponential backoff, max 8 attempts, then
> dead-letter + ops alert."

PRD-00 §0.3, replay:

> "**Replay cursor:** the external replay API (`GET /events`) orders by `seq` and
> serves only rows older than a **5-second watermark** … ULIDs remain the identity;
> `seq` is transport order only."

PRD-00 §0.2, cache consumer:

> "`claims.external_refs` is a denormalised read cache for joins/search only, updated
> by a single dedicated consumer subscribed to `field.updated` on `external.*` paths —
> nothing else writes it. Audit and grading always read the field, never the cache."

PRD-00 §0.6, ledger (DDL binding):

> "Separate append-only table `audit_ledger(id, seq BIGSERIAL, occurred_at, actor,
> action, claim_id, object_ref, before_hash, after_hash, detail JSONB, row_hash)`
> where `row_hash = SHA256(prev.row_hash ‖ canonical_json(row))` … Written for: every
> field version, every FSM transition, every LLM call …, every outbound email, every
> projection write, every autonomy change, every human review action. Nightly job
> re-verifies chain integrity and anchors the day's head hash into S3 Object-Lock (WORM)."

> "**Serialization (binding):** all ledger appends flow through the event spine to
> **one dedicated Celery queue with `concurrency=1`**; that single consumer assigns
> `seq` from its own counter and computes the hash chain. No other code path writes
> `audit_ledger`."

> "**On nightly verification failure — audit-degraded mode:** (a) autonomy promotions
> frozen platform-wide; (b) every L3+ action is additionally dual-written directly to
> the S3 WORM anchor until the chain is repaired and re-verified; (c) ops paged;
> (d) incident review is mandatory before promotions unfreeze. **No automatic
> demotions**."

PRD-00 §0.5, SLA engine:

> "`sla_definitions` ship in the pack: `{id, name, start_event, stop_event,
> warn_after, breach_after, escalate_to_role, calendar}`. **`calendar ∈ {24x7,
> send_window, business}`** … Beat tick every 5 minutes evaluates open clocks → emits
> `sla.warned` / `sla.breached` → PRD-04 renders + escalates. Every clock row keeps
> started/stopped timestamps — **this table is the outcome-pricing baseline dataset**;
> it is never purged."

Clock seed values (§0.5 table): acknowledge — 24x7, start `claim.created`, warn 30m,
breach 2h; doc_item_age — calendar days, warn 3d, breach 7d; assessor_turnaround —
business days, warn 3d, breach 5d; approval_dwell — business hours, warn 24h, breach
72h; repair_duration — calendar, warn 14d; bid_window — calendar, fixed 4d.

Section 0 ED-6a, PII (mechanics binding):

> "- When `claim_fields.pii_class != 'none'`, `value` is stored as an
>   envelope-encrypted blob (AES-256-GCM).
> - **DEK per claim**, wrapped by the KMS CMK, stored in `claims.dek_wrapped`. …
> - Equality-search paths … use a `value_search` blind-index column … = HMAC-SHA256 of
>   the normalised value under a dedicated KMS-held index key. Populated for exactly:
>   national ID, KRA PIN, DL number, phone, bank account number.
> - **Registration plates stay plaintext.** …
> - … every decrypt is access-logged with user id + field path."

PRD-00 §0.8, scenarios covered:

> "(3) kill worker mid-dispatch → no event lost, no duplicate side-effect (idempotency
> proven); (4) tamper a ledger row in staging → nightly verify fails loudly;
> (6) SLA breach fires within 5min of threshold."

## 3. Architecture & module map (all inside `platform/claim_core/`)

| Module | Contents |
|---|---|
| `outbox.py` | `Dispatcher`: consumer registry, `dispatch_once()`, delivery state machine, backoff schedule, dead-letter |
| `consumers.py` | built-in consumers: `ledger`, `external_refs`, `sla` |
| `ledger.py` | `LedgerWriter` (sole `audit_ledger` writer), `canonical_json`, `verify_chain()`, `anchor_head()`, audit-degraded mode |
| `sla.py` | definition registry (data), `SlaEngine`: start/stop on events, `evaluate(now)`, calendar math |
| `calendars.py` | `24x7` / `business` / `send_window` duration arithmetic + `holidays` data |
| `crypto.py` | `KeyProvider` protocol, `LocalKeyProvider`, envelope encrypt/decrypt, blind index HMAC, normalisation |
| `storage.py` | `BlobStore` protocol, `LocalBlobStore` (dev/test); S3 impl is the infra packet's |
| `celery_app.py` | Celery app + Beat schedule (dispatch 2s, sla tick 5m, ledger verify nightly) — thin wiring only, logic lives in the modules above |
| `models.py` (extend) | `AuditLedgerRow`, `SlaClock`, `SlaDefinitionRow` (if persisted), `PlatformState` |
| `alembic/versions/0002_*` | new tables |

Celery tasks must be one-line wrappers around module functions — **all logic must be
drivable synchronously in tests without a broker**.

## 4. Feature contracts

### 4.1 Dispatcher (§0.3)

- Consumer registry: `dispatcher.register_consumer(name: str, fn: Callable[[Event], None])`.
  Built-ins registered at `create_app` time: `"ledger"`, `"external_refs"`, `"sla"`.
- On `dispatch_once()`: select events lacking a `(event_id, consumer)` delivery row in
  `succeeded`/`dead_letter`, honouring `attempts` + next-retry backoff
  (1s→2s→4s… capped, **max 8 attempts**, then status `dead_letter` + an
  `ops.alert`-typed event row). On PostgreSQL the poll uses `FOR UPDATE SKIP LOCKED`;
  SQLite falls back to the serialized path (D-4 posture). Returns number of
  deliveries attempted.
- Delivery rows: statuses `pending|succeeded|failed|dead_letter`, `attempts`,
  `last_error` — per the §0.3 `event_deliveries` DDL (already migrated in 0001).
- Consumer exceptions: caught per delivery — one consumer failing must not block
  others or other events (at-least-once).
- Ledger consumer is **serialized**: within `dispatch_once` it processes events in
  `seq` order, single-threaded; the Celery route pins it to queue `ledger` with
  `concurrency=1` (config in `celery_app.py`).

### 4.2 Replay API (§0.3)

- `GET /events?after_seq=<int>` → `200 {"events": [{id, seq, claim_id, type, payload,
  actor, correlation_id, occurred_at}]}` ordered by `seq`, only rows with
  `occurred_at <= now - 5s`, `seq > after_seq`, limit 500. `after_seq` default 0;
  non-integer → 422.

### 4.3 external_refs cache (§0.2)

- Consumer `"external_refs"`: on `field.updated` where path starts `external.`, set
  `claims.external_refs[path] = value` (flat dot-path keys, D-11). No other code path
  writes `claims.external_refs` (extend the packet-1 create path to stop defaulting
  writes elsewhere if any). Hydration continues to read fields, never the cache.

### 4.4 Audit ledger (§0.6)

- `audit_ledger` DDL exactly as quoted. `id` ULID; `seq` assigned by the writer from
  its own counter (max+1 under the writer's serialization, **not** the DB sequence).
- Action mapping (this packet): `field.updated` → `field.version`; `field.verified`
  → `field.verified`; `claim.status_changed` → `fsm.transition`; `review.created` →
  `review.action`; `claim.created` → `claim.created`; `document.received` →
  `document.received`; `pii.decrypt` (direct append, see 4.6) → `pii.decrypt`.
  Unknown event types: **no ledger row, no error** — the map grows with later PRDs.
- `before_hash`/`after_hash`: sha256 of canonical_json of the referenced object state
  where the event payload carries it; else null (D-12).
- `canonical_json`: sorted keys, no whitespace, UTF-8. `row_hash =
  sha256(prev_row_hash_hex + canonical_json(row_without_row_hash))`; genesis prev = `""`.
- `verify_chain() -> {"ok": bool, "checked": int, "first_bad_seq": int | None}` —
  recomputes the full chain.
- `anchor_head()` — writes `{date, head_seq, head_hash}` to the `BlobStore` under
  `audit-anchors/<date>.json`.
- Nightly Beat job = `verify_chain` + `anchor_head`; on failure: set
  `platform_state['audit_degraded'] = true` and
  `platform_state['autonomy_promotions_frozen'] = true`, emit `ops.alert` event.
  (L3+ dual-write hook: expose `ledger.dual_write_required() -> bool`; nothing calls
  it yet — autonomy is PRD-03.) No automatic demotions — nothing else changes.
- `platform_state`: key TEXT PK, value JSONB, updated_at — new table (register #27).

### 4.5 SLA engine (§0.5)

- Definitions are **data**: `sla/definitions.yaml` + `sla/holidays.yaml` inside
  `claim_core` (moves to the motor pack when PRD-13 lands — path noted in the file
  header). Seed all six §0.5 clocks with the quoted values.
- `approval_dwell` ships `blocked_on_inputs` — "business hours" bounds are uncaptured
  (register #28): the definition row carries `status: blocked_on_inputs` and the
  engine refuses to start such clocks (visible, never guessed).
- Unstated `stop_event`s (e.g. acknowledge) are `pending_capture` (register #29):
  clocks start/warn/breach but only stop via explicit stop or claim reaching a
  suppressing state.
- `sla_clocks` table (register #27 — no DDL in PRD): `id` ULID, `claim_id`,
  `definition_id`, `started_at`, `stopped_at NULL`, `warn_at`, `breach_at`,
  `state ∈ {running, warned, breached, stopped}`, `started_by_event`,
  `stopped_by_event NULL`. **Never purged, never deleted** (ED-9).
- Consumer `"sla"`: on a definition's `start_event` → insert clock (compute
  warn_at/breach_at via the definition's calendar); on `stop_event` or a
  `claim.status_changed` into a `suppresses_activity` state → stop all open clocks
  for the claim (`sla.stopped` event).
- `evaluate(now)` (Beat: every 5 min): open clocks past `warn_at` and still `running`
  → `sla.warned` + state `warned`; past `breach_at` → `sla.breached` + state
  `breached`. Emits each at most once per clock. Scenario (6): a breach fires on the
  first evaluation after threshold — with the 5-min tick that is "within 5min".
- Calendars: `24x7` = wall clock; `business` = Mon–Fri excluding `holidays.yaml`
  dates, day-granularity arithmetic (register #28 records the Mon–Fri assumption);
  `send_window` durations count calendar time (the send-window constraint applies to
  *sends*, PRD-06's job). Holidays file: fixed-date KE statutory holidays only;
  movable feasts `pending_capture` (register #29).

### 4.6 PII encryption (ED-6a)

- `KeyProvider` protocol: `generate_dek() -> bytes`, `wrap(dek) -> bytes`,
  `unwrap(wrapped) -> bytes`, `index_hmac(normalised: str) -> str`.
  `LocalKeyProvider(master_key: bytes, index_key: bytes)` — keys from env
  `PACHA_LOCAL_MASTER_KEY`/`PACHA_LOCAL_INDEX_KEY` (base64) or ephemeral random when
  unset (tests). KMS provider = infra packet (register #30).
- Write path: field write with `pii_class != 'none'` → ensure claim DEK (generate +
  wrap + store `claims.dek_wrapped` on first PII write), store `value` as
  `{"__enc__": {"alg": "AES-256-GCM", "nonce": b64, "ct": b64}}`. Plaintext never
  hits the DB.
- Blind index: populated for **exactly** the five ED-6a field kinds; dictionary
  additions this packet (all `blind_index: true`): `parties.insured.national_id`
  (pii `sensitive`), `parties.insured.kra_pin` (`sensitive`), `parties.insured.dl_number`
  (`personal`), `parties.insured.bank_account` (`sensitive`) — plus existing
  `parties.insured.phone` (`personal`). Normalisation (register #31): uppercase, strip
  whitespace/hyphens; phones: digits only, `0`-prefix → `254`. `value_search =
  index_hmac(normalised)`.
- Read path: hydration decrypts transparently and **each decrypt appends a
  `pii.decrypt` ledger row directly via the LedgerWriter** (actor = X-Actor, detail =
  field path) — the one sanctioned direct append besides the consumer, same writer
  object, same chain (D-13). Timeline/replay payloads must never contain plaintext
  PII values (events carry paths + field ids, not values — already true; keep it true).
- Registration plates: **plaintext** — do not add `vehicle.reg` encryption when the
  motor pack registers it.

### 4.7 Documents + claim list API (§0.7)

- `POST /claims/{id}/documents` — multipart: `file` + form fields `source_channel`,
  `source_ref`. Stores bytes via `BlobStore` (`documents/<claim>/<ulid>`), computes
  `sha256`, `page_count` null (PRD-01's job), status `received`, `source` JSON from
  the form fields, emits `document.received` — one transaction with the row. →
  `201 {"id", "sha256", "status", "filename", "mime"}`. Same sha256 twice on one
  claim → `409 DUPLICATE_DOCUMENT` (dedupe per §0.2 comment; PRD-01 refines).
- `GET /claims/{id}/documents` → `200 {"documents": [...metadata, no bytes...]}`.
- `GET /claims?status=&lob=&sla_breached=` → `200 {"claims": [{id, lob, status,
  substatus, assigned_to, created_at, updated_at}]}`; filters conjunctive;
  `sla_breached=true` → claims with a clock in state `breached` and not `stopped`.
  Unknown status value → 422 (never guess).

### 4.8 `create_app` signature (extends packet-1 contract)

```python
create_app(
    database_url: str,
    *,
    clock: Callable[[], datetime] | None = None,   # injectable time (tests)
    key_provider: KeyProvider | None = None,        # default LocalKeyProvider
    blob_store: BlobStore | None = None,            # default LocalBlobStore(tmp)
) -> FastAPI
```

Exposed for tests/ops on `app.state`: `engine`, `dispatcher`, `sla_engine`, `ledger`.

## 5. CTO decisions (D-x) and register entries

- **D-9** — dispatcher logic broker-free and synchronous-drivable; Celery is wiring
  only. Tests never need Redis. (No register entry — implementation posture.)
- **D-10** — CI runs the suite against PostgreSQL 16 (service container) with Redis 7
  available; SQLite remains the no-env default. Closes the CI half of register #21.
- **D-11** — `external_refs` cache keys = flat dot-paths (`{"external.icon.claim_no":
  "..."}`) — mirrors canonical field paths, no nesting ambiguity. (Register #32.)
- **D-12** — `before_hash`/`after_hash` null where the event payload carries no
  object state; populated from payload state otherwise. (Register #32.)
- **D-13** — `pii.decrypt` ledger rows append via the same single LedgerWriter inline
  (not via the event spine): a decrypt is a read, produces no domain event, but must
  be access-logged synchronously. (Register #32.)
- **Register #27** — `sla_clocks` + `platform_state` DDL designed locally (PRD gives
  semantics, no DDL).
- **Register #28** — business-hours bounds uncaptured → `approval_dwell`
  `blocked_on_inputs`; business-day = Mon–Fri excl. holidays assumption.
- **Register #29** — movable KE holidays + unstated `stop_event`s `pending_capture`.
- **Register #30** — KMS/S3 providers deferred to infra packet; local providers carry
  the interface.
- **Register #31** — blind-index normalisation rules defined locally, need capture
  confirmation.

## 6. Builder guardrails

- **No payment ops, no L4 anything** — nothing in this packet touches money movement.
- The LedgerWriter is the only code path writing `audit_ledger` (guide §3.10) —
  reviewer greps for stray inserts.
- No new review-item *types* — everything stays `EXCEPTION` subtypes (guide §3.9).
- Event payloads/replay/timeline: no plaintext PII, ever.
- Alembic migration `0002` for the new tables; migration for existing tables only via
  register entry.
- All packet-1/2 acceptance tests keep passing unmodified.

## 7. Definition of done (ED-7/ED-7a)

- All acceptance tests in `tests/acceptance/test_packet_03_prd00_completion.py` pass
  unmodified; the full suite passes on SQLite (no env) **and** on PostgreSQL
  (`DATABASE_URL` set) — CI covers the PG leg.
- Unit ≥ 80% on `platform/claim_core/` — calendar math and chain verification get
  boundary tests (holiday-spanning durations; genesis row; single-row chain).
- Gates green: ruff, money-float lint, banned-calls, pytest.
- OpenAPI renders; runbook page `docs/runbooks/claim_core.md` (dispatcher recovery,
  audit-degraded procedure, key rotation note).
- ED-11: anything underdetermined → narrowest safe behaviour + register entry.
