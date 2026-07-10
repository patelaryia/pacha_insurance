# PACKET-01 — Claim substrate: data model, field store, hydration (PRD-00 slice 1)

> **Status:** issued · **Builder:** Codex per `AGENTS.md` · **Reviewer:** CTO per `CLAUDE.md`
> **Source spec:** `docs/PRD-00_Canonical_Claim_Object_and_Event_Spine_v1.1.md`. On any
> conflict, PRD-00 wins over this packet; Section 0 wins over PRD-00 (ED precedence).
> **Acceptance tests:** `tests/acceptance/test_packet_01_claim_substrate.py` — protected;
> the builder may not modify them. They are failing by design until this packet is done.

## 1. Scope

**In:** the PRD-00 §0.2 data model (all five tables), the core field dictionary
(packet-1 subset, §5 below), the append-only field write rule with transactional
event rows (write-side outbox only), atomic batch `PATCH`, optimistic write retry,
the hydration read, and the API subset: `POST /claims`, `GET /claims/{id}`,
`PATCH /claims/{id}/fields`, `GET /claims/{id}/timeline`.

**Out (later packets):** FSM transitions beyond the creation state (packet 2);
outbox **dispatcher**/consumers/`event_deliveries` consumption (packet 3); SLA
clock engine (packet 4); audit ledger + hash chain (packet 5); PII envelope
encryption + blind index population (ED-6a packet — `pii_class` is stored now,
encryption is not yet applied); `GET /events?after_seq=` replay API; AuthZ
(PRD-04 roles — interim actor mechanism in §6).

## 2. Binding spec quotes (implement these verbatim)

PRD-00 §0.2, table definitions — the DDL is column-level binding:

> ```sql
> CREATE TABLE claim_fields (
>   id                 TEXT PRIMARY KEY,       -- ULID
>   claim_id           TEXT NOT NULL REFERENCES claims(id),
>   path               TEXT NOT NULL,
>   value              JSONB NOT NULL,
>   value_type         TEXT NOT NULL,          -- 'string'|'money'|'date'|'datetime'|'bool'|'enum'|'object'
>   source_type        TEXT NOT NULL,          -- 'extraction'|'calc'|'rule'|'human'|'system'|'projection_readback'
>   source_ref         JSONB,
>   confidence         NUMERIC(4,3),           -- null for human/system sources
>   verification_state TEXT NOT NULL,          -- 'extracted'|'human_verified'|'system_confirmed'
>   pii_class          TEXT NOT NULL DEFAULT 'none',
>   value_search       TEXT,
>   version            INT NOT NULL,
>   superseded_by      TEXT REFERENCES claim_fields(id),
>   created_by         TEXT NOT NULL,          -- 'agent:intake'|'user:<ulid>'|'system'
>   created_at         TIMESTAMPTZ NOT NULL,
>   UNIQUE (claim_id, path, version)
> );
> CREATE INDEX ix_fields_current ON claim_fields (claim_id, path) WHERE superseded_by IS NULL;
> ```

(`claims`, `documents`, `communications`, `parties`, `events` likewise — full DDL in
PRD-00 §0.2/§0.3; implement every column, name and comment, exactly.)

PRD-00 §0.2, field dictionary:

> "Field dictionary: the platform ships a **core dictionary** (path → type, PII class,
> validation) covering `policy.*`, `loss.*`, `intimation.*`, `parties.*`, `reserve.*`,
> `settlement.*`; packs register extensions (`vehicle.*`, `assessment.*`, `salvage.*`
> from the Motor Pack). Writes to unregistered paths are rejected
> (`422 FIELD_NOT_IN_DICTIONARY`) — this is what keeps LOB packs honest."

PRD-00 §0.2, write rule:

> "**Write rule (hard invariant):** nothing updates a field in place. A new version row
> is written, prior version's `superseded_by` set, and a `field.updated` event emitted —
> one transaction. Human values always supersede agent values; an agent may never
> supersede a `human_verified` field (attempt → `409 HUMAN_OVERRIDE_PROTECTED` +
> review item)."

PRD-00 §0.2, write concurrency:

> "**Write concurrency (binding repository behaviour):** field writes are optimistic —
> on unique-violation (two writers raced the same `(claim_id, path, version)`), re-read
> the current version, reapply, max 3 retries, then error. `PATCH /claims/{id}/fields`
> batches are **atomic**: one transaction holding a per-claim advisory transaction lock
> (`pg_advisory_xact_lock(hashtext(claim_id))`) — all-or-nothing, serialized per claim."

PRD-00 §0.3, outbox (write side only in this packet):

> "Mechanics: **transactional outbox**. Event rows are written in the same transaction
> as the state change."

PRD-00 §0.8, acceptance scenarios covered by this packet:

> "(1) create claim, write 50 field versions incl. supersessions, hydrate returns
> exactly current values with full provenance; (2) attempt agent overwrite of
> human-verified field → 409 + review item."

ED-8 (Section 0): Money = BIGINT KES cents end to end; never floats.

## 3. Deliverable

One Python package: **`platform/claim_core/`**, import name `claim_core`
(see CTO decision D-1). Public interface consumed by the acceptance tests:

```python
from claim_core.app import create_app
app = create_app(database_url: str)   # returns a FastAPI app, schema created/migrated
```

SQLAlchemy 2 models + Alembic migration for all §0.2/§0.3 tables. FastAPI routes
per §1 scope. Pydantic v2 request/response models. ULIDs for every id
(`python-ulid`). UTC timestamps.

## 4. API contract (packet-level; OpenAPI is generated from it)

- `POST /claims` — body `{"lob": "motor", "pack_version": "motor@1.3.0"}` →
  `201 {"id", "lob", "pack_version", "status": "INTIMATED", "created_at", ...}`.
  Creation state is `INTIMATED` (PRD-00 §0.4 initial state). Emits `claim.created`
  event row in the same transaction.
- `PATCH /claims/{id}/fields` — body `{"writes": [{"path", "value", "value_type",
  "source_type", "source_ref"?, "confidence"?, "verification_state", "pii_class"?}]}`.
  Atomic batch: all writes in one transaction; any failure rejects the whole batch.
  Each write: new version row, supersession of prior current, `field.updated` event
  row, same transaction. → `200 {"results": [{"path", "field_id", "version"}]}`.
- `GET /claims/{id}` — hydrated object: `{"id", "lob", "pack_version", "status",
  "substatus", "assigned_to", "created_at", "updated_at", "fields": {<path>:
  {"value", "value_type", "version", "verification_state", "source_type",
  "source_ref", "confidence", "created_by", "created_at"}}}` — exactly the current
  (non-superseded, highest-version) value per path, full provenance.
- `GET /claims/{id}/timeline` — `200 {"events": [{"id", "type", "payload", "actor",
  "correlation_id", "occurred_at"}]}` ordered by `occurred_at` then `seq`.
- Errors: JSON body `{"code": "<MACHINE_CODE>", "detail": "<human text>"}`.
  Codes in this packet: `FIELD_NOT_IN_DICTIONARY` (422), `HUMAN_OVERRIDE_PROTECTED`
  (409), `MONEY_NOT_INTEGER_CENTS` (422), `CLAIM_NOT_FOUND` (404),
  `VALUE_TYPE_MISMATCH` (422).
- Actor: interim header `X-Actor` (D-3), format `agent:<name>` | `user:<ulid>` |
  `system`; becomes `created_by` on field rows and `actor` on events. Missing
  header → 422.

## 5. Core dictionary — packet-1 subset (D-2; authoritative for this packet)

| path | value_type | pii_class |
|---|---|---|
| `policy.number` | string | none |
| `policy.excess` | money | none |
| `loss.date` | date | none |
| `loss.description` | string | none |
| `intimation.channel` | enum (`email`,`phone`,`broker`,`portal`) | none |
| `intimation.received_at` | datetime | none |
| `parties.insured.name` | string | personal-low |
| `parties.insured.phone` | string | personal |
| `reserve.total` | money | none |
| `settlement.amount` | money | none |

Dictionary lives as **data** (config over code, guide §4): a registry the pack
extension mechanism can later add to. Unregistered path → `422 FIELD_NOT_IN_DICTIONARY`.
Money values must be JSON integers (KES cents, ED-8); any float → `422
MONEY_NOT_INTEGER_CENTS`. `verification_state = human_verified` requires
`source_type = human` and an actor of form `user:*` — else `422`.

Review item on 409: PRD-04's queue does not exist yet. Narrow behaviour (ED-11):
emit a `review.created` event row in the same transaction, payload
`{"type": "EXCEPTION", "subtype": "human_override_attempt", "path": ..., "attempted_by": ...}`.
Packet for PRD-04 will consume this.

## 6. CTO decisions recorded (D-x) and register entries

- **D-1** — `platform` shadows a Python stdlib module; packages live at
  `platform/<import_name>/` with `pythonpath = ["platform", "agents", "packs"]` in
  `pyproject.toml`; PRD-00's package import name is `claim_core`. (Register #18.)
- **D-2** — the PRDs never enumerate the core dictionary's exact path list; §5 above
  is the packet-1 subset, full dictionary is an open item. (Register #19.)
- **D-3** — AuthZ is PRD-04 scope; interim `X-Actor` header carries actor identity
  until then. (Register #20.)
- **D-4** — acceptance tests run on SQLAlchemy against SQLite by default
  (`DATABASE_URL` env overrides, e.g. Postgres locally); PG-only behaviours
  (`pg_advisory_xact_lock`, `JSONB`, partial index) must be implemented for the
  postgresql dialect with a documented SQLite fallback (plain serialized
  transaction; `JSON` variant), and get PG-backed CI in the infra packet.
  (Register #21.)

## 7. Definition of done (ED-7/ED-7a)

- All acceptance tests in `tests/acceptance/test_packet_01_claim_substrate.py` pass,
  unmodified.
- Unit tests ≥ 80% coverage on `platform/claim_core/`.
- Alembic migration included and reviewed.
- OpenAPI spec generated (`app.openapi()` renders without error; committed artifact
  not required this packet).
- `ruff check .`, `python tools/ci/money_float_lint.py`,
  `python tools/ci/banned_calls.py`, `pytest -q` all green.
- Anything underdetermined: narrowest safe behaviour + register entry (ED-11) —
  never a local judgement call.
