# PACKET-20 — Projection substrate and permanent paste-assist mode (PRD-09 slice 1 of 3)

> **Status:** issued · **Builder:** Codex per `AGENTS.md` · **Reviewer:** CTO per
> `CLAUDE.md`
>
> **Source spec:** `docs/PRD-09_System_Projection_and_Reconciliation_v1.1.md`
> §9.1–§9.3, paste-assist portion of §9.6, and acceptance scenario 1 in
> §9.7; PRD-00 §0.2/§0.3/§0.4; PRD-03 §3.4; PRD-04 §4.2–§4.5; PRD-07
> §7.5; PRD-08 §8.3; Section 0.5 AR-1/AR-2; Section 0
> ED-1/ED-6/ED-6a/ED-7/ED-8/ED-9/ED-10/ED-11; guide §3/§4/§6; registers
> #1–#3/#17/#26/#30/#71/#78/#101/#117/#208/#217/#224/#252/#261–#273.
>
> **Depends on:** PACKET-19 merged and green.
>
> **Acceptance:** new protected backend and console Packet-20 suites. The
> builder does not weaken or rewrite Packet-01–19 acceptance fixtures.
>
> **Next packet:** PACKET-21 — adapter boundary, outbound-only RPA runner,
> reconciliation, drift detection, evidence, fallback, and adapter health.

## 0. CTO disposition and slice boundary

PRD-09 is cut into three packets:

1. **PACKET-20:** durable projection substrate + permanent paste-assist mode;
2. **PACKET-21:** RPA runtime + the zero-silent-divergence control plane;
3. **PACKET-22:** captured ICON/EDMS operation activation and live acceptance.

This packet provides a useful, permanently supported human path without making
an external-system write from platform code. It owns:

- the exact PRD-09 `projections` table;
- the complete v1 operation catalogue and canonical capability ids;
- payload snapshots with exact field values and versions;
- deterministic idempotency;
- consumption of the existing reserve `projection.requested` event;
- authenticated projection reads;
- a click-path-derived field strip;
- per-screen completion, attestation, paste duration, and inline readback;
- canonical `external.icon.claim_no` commit;
- `REPORT_RECEIVED→REGISTERED` after verified ICON readback;
- deterministic 10% `PASTE_READBACK_CHECK` creation;
- the Claim-360 Systems workspace.

It does **not** add an adapter `execute`, Playwright, a runner process, browser
selectors that hunt or recover, screenshots, automatic reconciliation, nightly
drift, adapter health, or any live target-system path. The production motor
operation catalogue is honest: paths not captured under open item 3 remain
`pending_capture`; future PRD-11/12 operations remain `blocked_on_inputs`.

The packet is complete when the mechanics pass with a synthetic fixture
click-path. That does not discharge PRD-09's live ICON acceptance scenario.
Nothing in the production pack becomes executable by inventing a form order,
selector, value map, target unit, or claim-number regex.

## 1. Deliverables and package boundary

Add:

```text
agents/projection_agent/
  __init__.py          # public builder/facade only
  models.py            # projections table
  config.py            # operation + click-path registry
  service.py           # request/snapshot/idempotency/lifecycle
  paste.py             # field-strip/readback mechanics
  tasks.py             # weekly deterministic paste readback sampling
  api.py               # authenticated console routes

packs/motor/projection/
  operations.yaml      # complete v1 operation catalogue, mode + availability

platform/claim_core/alembic/versions/0015_projections.py
tests/acceptance/test_packet_20_projection_paste_assist.py
tests/acceptance/console/test_packet_20_console.test.tsx
docs/runbooks/projection_paste_assist.md
```

Public construction:

```python
build_projection_agent(app, *, operation_root: Path | None = None)
    -> ProjectionAgent
```

Construction requires, in order, `claim_service`, `dispatcher`,
`eval_harness`, `review_queue`, `agent_runtime`, and installed console
identity/RBAC. It is idempotent and stores the facade on
`app.state.projection_agent`.

Public facade:

```python
class ProjectionAgent:
    operations: OperationRegistry

    def request(
        self,
        *,
        claim_id: str,
        operation: str,
        actor: str,
        source_event_id: str | None = None,
    ) -> ProjectionResult: ...

    def backfill(self, *, actor: str = "system") -> int: ...
    def get(self, projection_id: str, *, actor: str) -> ProjectionView: ...
    def list_for_claim(self, claim_id: str, *, actor: str) -> list[ProjectionView]: ...
```

Cross-package access uses curated package roots or `app.state` only. No package
imports `claim_core.models`, `review_queue.models`, or another package's private
module. The PostgreSQL session fixture explicitly imports
`projection_agent` so migration/model drift cannot hide behind lazy table
creation.

## 2. Exact persistence contract

Implement PRD-09 §9.2 verbatim:

```sql
CREATE TABLE projections (
  id TEXT PRIMARY KEY,
  claim_id TEXT NOT NULL,
  operation TEXT NOT NULL,
  mode TEXT NOT NULL,
  status TEXT NOT NULL,
  payload JSONB NOT NULL,
  readback JSONB,
  divergence JSONB,
  evidence JSONB,
  attempts INT DEFAULT 0,
  idempotency_key TEXT UNIQUE,
  created_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ
);
```

Binding rules:

- add no columns, foreign keys, timestamps, soft-delete fields, job leases, or
  target-system ids not present above;
- JSONB maps to the repository's existing PostgreSQL/SQLite JSON type;
- `attempts` has database default 0 but is not used as an RPA retry counter in
  this packet;
- service validation closes mode to `paste_assist|rpa|api` and status to
  `queued|executing|verifying|completed|failed|diverged`;
- PACKET-20 creates only `paste_assist` rows;
- rows are never deleted and payload snapshots are never mutated;
- `mode`, `status`, `readback`, `divergence`, `evidence`, `attempts`, and
  `completed_at` are the only mutable projection columns;
- status changes take a row lock on PostgreSQL and an application-local
  projection lock on SQLite; a stale or illegal edge returns 409 and writes
  nothing.

PACKET-20 legal status edges:

```text
queued -> executing        first explicit paste-assist start
executing -> executing     group checkbox update
executing -> verifying     accepted final attestation
verifying -> completed     declared readbacks committed
```

`failed` and `diverged` are reserved for PACKET-21 except that a structurally
invalid persisted row found at runtime fails closed to `failed` with a
`projection.failed` event. Configuration that is merely `pending_capture`
creates no invalid snapshot row and is not called a runtime failure.

Migration 0015 is reviewed on SQLite and PostgreSQL, including exact columns,
JSONB dialect mapping, unique idempotency key, defaults, upgrade, and
downgrade.

## 3. Operation and capability catalogue

`packs/motor/projection/operations.yaml` has this top-level shape and registers
exactly these 15 operation ids:

```yaml
version: 1
paste_readback_sampling:
  rate_percent: 10
  schedule:
    day_of_week: mon
    hour: 8
    minute: 0
    timezone: Africa/Nairobi
operations: []
```

The weekly schedule is pack data. The production launch value is Monday 08:00
EAT; changing it is a pack/configuration change, never code.

```text
icon.policy_read
icon.claim_register
icon.reserve_create
icon.reserve_breakdown
icon.reserve_adjust
icon.assessor_payment_request
icon.note_entry
icon.claim_details_report
icon.salvage_register
icon.payment_voucher
edms.general_payments
edms.claims_workflow
edms.attach_and_tag
edms.claim_payment
edms.payment_workflow
```

Each row contains exactly:

```yaml
id: icon.claim_register
version: 1.0.0
system: icon
mode: paste_assist
status: pending_capture
blocked_on: open-item-3
click_path_ref: null
owner_prd: PRD-09
```

Allowed keys and values:

- `id`: one exact registered operation;
- `version`: strict `major.minor.patch`, advanced whenever mode, input mapping,
  click-path ref, target encoding, validator, or output contract changes;
- `system`: `icon|edms`;
- `mode`: `paste_assist|rpa|api`;
- `status`: `live|pending_capture|blocked_on_inputs`;
- `blocked_on`: non-empty when status is not `live`, null when live;
- `click_path_ref`: repository-relative YAML ref when live, null otherwise;
- `owner_prd`: `PRD-09|PRD-11|PRD-12`.

Startup rejects an unknown key, missing/extra operation, duplicate id,
system/id mismatch, duplicate/bad version, unsupported mode, invalid sampling
configuration, path traversal, a live row without a valid same-operation/
same-version click path, or a blocked row without a blocker.

Production availability:

- all uncaptured PRD-09 paths are `pending_capture` on open item 3;
- `icon.reserve_adjust` names open item 17;
- `icon.salvage_register` is `blocked_on_inputs` on PRD-11;
- `icon.payment_voucher`, `edms.claim_payment`, and
  `edms.payment_workflow` are `blocked_on_inputs` on PRD-12/GP-1;
- no future-owned row can be switched live by PACKET-20.

The existing `packs/motor/approval_pack/icon.yaml` `icon.note_entry` slot stays
unchanged and empty. It is the visible PRD-08 hand-off, not a second field
order. PACKET-22 replaces the slot and operation ref together when the path is
captured.

Canonical capability id:

```text
project.<operation>
```

Examples:

```text
project.icon.claim_register
project.edms.claims_workflow
```

Operation ids and capability ids are never interchangeable in events, review
items, agent runs, or autonomy queries. Fresh databases seed only canonical
`project.*` projection capability ids. The six provisional bare ids from
PACKET-08 are removed from pack policy data. Because no projection executor
has existed, migration/startup must prove they have no durable production
evidence before removing them; any database with such evidence fails closed
with `LEGACY_PROJECTION_CAPABILITY_IN_USE` for owner migration.

Paste-assist capabilities launch at L1 because the officer performs the target
write. Future PRD-11/12 rows stay L0 until their owner packet. Constitutional
ceilings remain code, not pack data:

- claim/policy read, claim register, note entry, report, claims workflow, and
  attach/tag: max L4;
- reserve create/breakdown/adjust, assessor-payment request,
  general-payments, and salvage-register: max L3;
- payment voucher and both EDMS payment workflows: max L2 until PRD-12's gate
  opens, and never above the applicable money-adjacent constitution.

Paste-assist does not call `execute_or_stage`: no agent or platform adapter is
performing the external write. The L1 evidence is the authenticated officer
start, screen confirmations, and final attestation. PACKET-21 alone registers
an adapter executor behind AR-2.

## 4. Click-path-to-field-strip contract

There is one versioned click-path YAML file per live operation. PACKET-20 reads
the human-facing subset; PACKET-21 later executes the same parsed definition.
There is no separate field-strip registry.

Minimum live definition:

```yaml
operation: icon.claim_register
version: 1.0.0
status: live
preconditions:
  - {assert: logged_in}
screens:
  - {id: claim_details, label: Claim details, order: 1}
steps:
  - id: s1
    screen: claim_details
    action: fill
    selector: "#policyNo"
    value: "{policy.number}"
    paste_assist:
      label: Policy number
      copy: true
readback:
  - capture: claim_number
    label: ICON claim number
    into: external.icon.claim_no
    assert_format: icon_claim_no_regex
failure_policy: screenshot_always, halt_on_selector_miss, no_guessing
```

PACKET-20 validates but does not execute `preconditions`, `selector`, browser
actions, or `failure_policy`.

For the paste strip:

- screens are ordered by integer `order`, unique and contiguous from 1;
- every paste-visible step references one declared screen;
- steps retain YAML order within the screen;
- only `fill|select` steps with a declared value binding appear as copy rows;
- labels are pack data and non-empty;
- a field placeholder must resolve to a registered canonical field path;
- the exact field id, version, value type, verification state, and stored value
  enter the immutable payload snapshot;
- missing or under-verified input returns `blocked_on_inputs`; it never renders
  a blank row;
- rule bindings must cite one completed rule run and its version; a missing or
  ambiguous run blocks;
- generated values, literals, value maps, date formats, and money target units
  must be explicitly declared by captured configuration before a production
  path becomes live;
- money remains integer KES cents in the stored payload. If the target expects
  shillings, the click path must declare that boundary encoding; conversion is
  exact decimal division by 100 with no rounding, commas, or currency prefix;
- without a declared target encoding, a money copy row blocks rather than
  copying cents into a shilling field;
- the Clipboard API receives the exact `copy_value` string returned by the
  server. The browser applies no formatting or transformation.

The loader rejects duplicate step ids, duplicate screen ids/orders, unknown
actions, unresolved fields/rules, non-canonical money bindings, undeclared
target encodings, a readback outside the external-field dictionary, or any
path that escapes the operation root.

Fixture packs may supply complete synthetic paths and validators. Production
motor files remain absent/empty while open item 3 is unresolved.

## 5. Payload snapshot, PII, and idempotency

Projection payload schema:

```json
{
  "schema_version": 1,
  "operation_definition": {
    "operation": "icon.claim_register",
    "version": "1.0.0"
  },
  "fields": [
    {
      "step_id": "s1",
      "path": "policy.number",
      "field_id": "01...",
      "version": 3,
      "value_type": "string",
      "verification_state": "human_verified",
      "value": "POL-123",
      "external_encoding": "raw"
    }
  ],
  "source_event_id": "01...",
  "snapshot_hash": "<sha256>"
}
```

`snapshot_hash` is SHA-256 of sorted, compact UTF-8 JSON over
`{operation_definition, fields}` with the hash member absent. `fields` stay in
click-path order in storage, but canonical JSON sorts object keys.

Idempotency key:

```text
<claim_id>:<operation>:<snapshot_hash>
```

Therefore:

- exact redelivery returns the existing projection;
- the same value in a new field version creates a new projection;
- a changed operation-definition version creates a new projection;
- corrected C-02 inputs produce a new reserve projection, preserving register
  #217;
- transport/event redelivery cannot double-create;
- the client cannot supply a payload, hash, field version, mode, or key.

PII rules:

- `payload` is never an escape hatch around ED-6a;
- values whose field definition requires encryption are stored as the existing
  claim-DEK AES-256-GCM envelope, not plaintext;
- registration plates retain the ED-6a plaintext exception;
- add curated `ClaimService` snapshot/read methods that return exact selected
  field versions while preserving their stored encrypted form, and decrypt
  only for an authenticated paste view;
- every projection-payload PII decrypt emits the existing `pii.decrypted`
  event with field path and actor;
- API responses are private/no-store and role-checked;
- events, review payloads, logs, and ledger rows carry ids, paths, versions,
  hashes, and readback-path names only—never copied values or decrypted PII.

No package imports encryption helpers privately or wraps its own keys.

## 6. Request creation and existing event consumption

Register consumer `projection_agent` on `projection.requested`.

Canonical future event payload:

```json
{
  "operation": "icon.claim_register",
  "claim_id": "01...",
  "source_refs": {}
}
```

The consumer ignores client values and rebuilds the snapshot from the declared
operation inputs.

PACKET-17's existing, earlier payload is fixed:

```json
{
  "claim_id": "01...",
  "calc_run_id": "01...",
  "reserve_total": 14265600
}
```

When and only when that exact legacy shape is present, map it to
`icon.reserve_create`, verify the cited executed C-02 run and current
`reserve.total` field version, and snapshot from durable sources. Never trust
the event's `reserve_total` without matching both. C-03 is currently blocked,
so no `icon.reserve_breakdown` projection is fabricated.

Other automatic operation triggers are PACKET-22. PACKET-20 exposes the public
`request` method for fixture acceptance and future producer packages but adds
no officer endpoint that can invent an operation or payload.

If an operation is `pending_capture`/`blocked_on_inputs`, return a fail-visible
`ProjectionResult{status: "blocked_on_inputs", blocked_on}` and create no
projection row: without a valid definition there is no lawful field/value/
version snapshot to persist. The operation catalogue still makes the blocker
visible in Systems.

`build_projection_agent` performs an idempotent history backfill of
`projection.requested`. When a formerly blocked operation definition is
captured under a new version, the next build/backfill can create the projection
from the immutable source event. The event is therefore not lost and no queued
payload is mutated. Unknown operations are rejected and never stored.

## 7. Authenticated paste-assist API

Add:

```http
GET  /console/claims/{claim_id}/projections
GET  /console/claims/{claim_id}/projections/{projection_id}/paste-assist
POST /console/claims/{claim_id}/projections/{projection_id}/paste-assist/start
PUT  /console/claims/{claim_id}/projections/{projection_id}/paste-assist/groups/{group_id}
POST /console/claims/{claim_id}/projections/{projection_id}/paste-assist/confirm
```

All routes use the installed Entra/X-Actor boundary. Operational roles may
read; `claims_officer`, `asst_claims_manager`, and `claims_manager` may operate
the strip. `auditor` is read-only. Unknown/cross-claim projection ids return
404, not an existence leak.

List response:

```json
{
  "operations": [
    {
      "id": "icon.claim_register",
      "capability_id": "project.icon.claim_register",
      "system": "icon",
      "mode": "paste_assist",
      "status": "pending_capture",
      "blocked_on": "open-item-3"
    }
  ],
  "projections": [
    {
      "id": "01...",
      "operation": "icon.reserve_create",
      "mode": "paste_assist",
      "status": "queued",
      "snapshot_hash": "...",
      "blocked_on": "open-item-3",
      "created_at": "...",
      "completed_at": null
    }
  ]
}
```

Paste view:

```json
{
  "projection_id": "01...",
  "operation": "icon.claim_register",
  "definition_version": "1.0.0",
  "status": "executing",
  "groups": [
    {
      "id": "claim_details",
      "label": "Claim details",
      "done": false,
      "fields": [
        {
          "step_id": "s1",
          "label": "Policy number",
          "path": "policy.number",
          "copy_value": "POL-123",
          "value_type": "string",
          "field_version": 3
        }
      ]
    }
  ],
  "readback_fields": [
    {
      "label": "ICON claim number",
      "path": "external.icon.claim_no",
      "required": true
    }
  ],
  "attestation_text": "I entered the values exactly as shown.",
  "started_at": "...",
  "elapsed_seconds": 42
}
```

Start:

- requires `queued`, live path, resolved inputs, and `mode=paste_assist`;
- records `paste_started_at` and actor under `evidence.paste_assist`;
- changes status to `executing`;
- exact repeat by the same/another authorised actor returns current state and
  does not reset the clock.

Group update body:

```json
{"done": true}
```

It is reversible before final confirmation, records the current done map and
last actor/time in evidence, and cannot modify the payload.

Confirm:

```http
Idempotency-Key: <non-empty>

{
  "attested": true,
  "readback": {
    "external.icon.claim_no": "ICON-..."
  }
}
```

Binding behaviour:

- every group must be done;
- attestation must be literal `true`;
- accept exactly the declared readback keys, no extras;
- validate each readback against the captured validator;
- no readback value is accepted when its format remains `pending_capture`;
- server records attested actor/time, end time, and elapsed whole seconds;
- same idempotency key + same body returns the first result;
- reused key + different body returns 409 `IDEMPOTENCY_CONFLICT`;
- a completed projection is immutable: exact repeat returns it, differing
  readback returns 409 `PROJECTION_ALREADY_COMPLETED`;
- request bodies and clipboard values never enter events or logs.

## 8. Readback, completion, and FSM

For `icon.claim_register`, successful confirm appends:

```text
path                 external.icon.claim_no
source_type          projection_readback
verification_state   system_confirmed
source_ref           {
  projection_id,
  operation,
  operation_version,
  attested_by,
  attested_at
}
```

Use the existing `ClaimService.write_fields` path so dictionary enforcement,
append-only versions, actor attribution, field events, and the
`external_refs` consumer remain authoritative.

Crash-safe finalisation:

1. lock/read projection and persist accepted attestation as
   `status=verifying`;
2. write the canonical external field;
3. on resume, recognise an exact current field whose source_ref names this
   projection and never append it twice;
4. mark projection completed with readback-path names and encrypted/allowed
   stored readback values;
5. emit one `projection.completed`;
6. dispatch the normal event spine; `external_refs` updates only through its
   existing consumer.

If a different current `external.icon.claim_no` is human-verified or belongs
to another projection, stop in `verifying` and create
`EXCEPTION{subtype: projection_readback_conflict}`. Never supersede it or pick
one.

After completed `icon.claim_register`:

- if claim status is `REPORT_RECEIVED`, call the existing FSM transition to
  `REGISTERED` with projection/readback refs;
- if already `REGISTERED` from the same readback, replay is a no-op;
- any other state is not auto-transitioned; the completed projection remains
  visible and an `EXCEPTION{subtype: projection_state_mismatch}` is created;
- PACKET-20 does not transition `REGISTERED→RESERVED`; projection is a
  parallel tracker, never that guard.

For all other paste-assist operations, officer attestation stands in as the
PRD-09 paste readback and completes the row; target-system reconciliation
begins in PACKET-21.

`projection.completed` contains:

```json
{
  "projection_id": "01...",
  "operation": "icon.claim_register",
  "mode": "paste_assist",
  "snapshot_hash": "...",
  "readback_paths": ["external.icon.claim_no"],
  "attested_by": "user:...",
  "attested_at": "...",
  "paste_seconds": 42
}
```

No field values appear in the event.

## 9. Paste readback sampling

Register a named weekly Celery task and Beat schedule from
`paste_readback_sampling`. The task scans completed paste-assist projections;
volume is bounded by the platform design envelope, so no new watermark table or
column is invented. It skips a projection that already has a
`PASTE_READBACK_CHECK` source event and selects the remainder using the existing
exact deterministic selector:

```text
sha256(projection_id) % 100 < 10
```

Selected rows create exactly one `PASTE_READBACK_CHECK` review item assigned by
the existing owner-routing rules. Concurrent/repeated weekly scans dedupe on
projection id before emitting `review.created`. Payload includes:

```json
{
  "projection_id": "01...",
  "operation": "icon.claim_register",
  "capability_id": "project.icon.claim_register",
  "snapshot_hash": "...",
  "readback_paths": ["external.icon.claim_no"]
}
```

It contains no copied values. The authenticated workspace resolves detail from
the projection service. Duplicate event dispatch or a later weekly scan creates
no second item.

PACKET-20 proves selection and workspace visibility. PACKET-21 owns mismatch
comparison, divergence state, screenshots/evidence, and the rule that no
divergence is auto-corrected.

## 10. Claim-360 Systems workspace

Replace the PRD-09-unavailable state in Claim 360's existing **Systems** tab.
Do not add an eighth tab.

Render:

- ICON and EDMS operation catalogue with mode, availability, blocker, and owner
  PRD;
- claim projections newest first;
- queued/executing/verifying/completed status;
- immutable snapshot version/hash metadata;
- screen progress for executing paste rows;
- attested actor and EAT timestamp after completion;
- elapsed paste time;
- canonical external readback paths, never raw PII;
- explicit `pending_capture`/`blocked_on_inputs`, never an empty panel.

For an executable paste row:

- open the field strip inside the Systems tab;
- render groups in configured order;
- each copy button has an accessible name containing its field label;
- successful copy announces `Copied <label>` in a polite live region;
- copy failure is visible and leaves the field unchanged;
- group checkboxes are keyboard operable;
- final confirm is disabled until all groups are done, required readbacks pass
  client-side shape checks, and attestation is checked;
- the server remains authoritative; 409/422 responses restore server state and
  never show optimistic completion;
- use EAT rendering and the existing integer-money display helper for labels,
  while clipboard content remains exact server `copy_value`;
- no global keyboard shortcut and no target-system iframe.

At 1366×768, the strip and claim context remain usable without horizontal page
scroll. The field list may scroll inside its workspace.

The S-6 Admin adapter-health placeholder remains until PACKET-21. Operation
availability is not adapter health and must not be presented as such.

## 11. Events, audit, security, and retention

Use only the existing PRD-00 projection event catalogue:

- `projection.requested`
- `projection.completed`
- `projection.failed`
- `projection.diverged` (PACKET-21 only)

Add `projection.completed` and `projection.failed` to the existing ledger
action map; `projection.requested` is already mapped. Group toggles do not
create new event types; the final completion event carries the attestation and
duration facts.

All field writes continue through claim core. All ledger appends continue
through the single writer. No package writes `audit_ledger`,
`claims.external_refs`, or an existing claim-field row directly.

Security:

- authenticated staff routes only; no public/portal route;
- operational role checks on every read/write;
- no raw blob key, target credential, selector, decrypted PII, or clipboard
  value in timeline/audit/error responses;
- projection PII at rest uses the claim DEK;
- production PII never enters fixture/synthetic acceptance;
- `Cache-Control: private, no-store` and `X-Content-Type-Options: nosniff`;
- a cross-claim id is 404 and is access-denied without disclosing its owner.

Retention follows the parent claim's seven-year crypto-shred posture. Projection
rows are not purged independently. Deleting the claim DEK makes encrypted
snapshot values unreadable.

## 12. Acceptance

Protected backend tests pin:

1. migration 0015 has the exact PRD-09 columns/default/unique key on SQLite and
   PostgreSQL, with upgrade/downgrade;
2. the catalogue has exactly 15 operation ids; unknown/missing/duplicate rows,
   bad refs, and live-without-path fail startup;
3. operation id and `project.<operation>` capability id stay distinct; fresh
   seeds have the pinned launch levels and constitutional ceilings;
4. the legacy PACKET-17 reserve request maps only to
   `icon.reserve_create`, verifies C-02/current reserve, and exact replay
   creates one row;
5. same claim/operation/snapshot returns one projection; a new field version
   or operation-definition version creates a new one;
6. encrypted PII is not plaintext in `projections.payload`; authorised view
   decrypts and access-logs it;
7. pending production operation is visible; request returns
   `blocked_on_inputs`, creates no invalid projection snapshot, and later
   backfill creates one row after a fixture definition becomes live;
8. fixture `icon.claim_register` renders groups/fields in click-path order,
   with exact unformatted clipboard strings and no missing/under-verified
   blank;
9. money copy blocks without target unit and converts cents exactly only when
   the fixture declares shilling encoding;
10. start is idempotent, group toggles are reversible before confirm, and
    elapsed time begins at explicit start;
11. confirm requires all groups + literal attestation + exact declared
    readback, rejects unknown/malformed keys, and is request-idempotent;
12. kill between readback field commit and projection completion resumes to
    one field version, one completed event, and one cache update;
13. an existing conflicting/human-verified ICON claim number is never
    superseded and creates one visible EXCEPTION;
14. ICON completion transitions `REPORT_RECEIVED→REGISTERED` once, but never
    owns `REGISTERED→RESERVED`;
15. the configured weekly sampler applies deterministic 10% selection and
    creates exactly one `PASTE_READBACK_CHECK`, while repeat weeks create no
    duplicate and a non-selected id creates none;
16. events, reviews, ledger, timeline, and API errors contain no copied PII or
    readback value.

Protected console tests pin:

1. existing seven Claim-360 tabs remain exact and Systems no longer shows the
   PRD-09 unavailable placeholder after installation;
2. operation catalogue and empty/projection states are explicit;
3. ordered group/field strip, Clipboard API exact value, accessible copy
   feedback, copy failure, and keyboard checkboxes;
4. readback + attestation gating and server 409/422 recovery;
5. queued/executing/verifying/completed rendering, EAT timestamps, and elapsed
   time;
6. no optimistic completion on server failure;
7. axe pass and usable 1366×768 layout.

Full regression additionally pins:

- all Packet-01–19 tests unchanged;
- AR-2 banned-call guard still fails any `adapter.execute` outside the gate;
- operation registry contains no funds-transfer verb;
- existing approval-pack items 12/13 still resolve by officer upload while
  projection-artifact capture remains PACKET-21/22;
- `packs/motor/approval_pack/icon.yaml` remains `pending_capture` and empty.

Live/manual gate, not discharged by synthetic CI:

- captured ICON claim-registration order, labels, dropdown mappings, units, and
  claim-number regex;
- full registration against ICON staging/training if it exists;
- if no test instance, the first live execution remains subject to PRD-09's
  L2 watched-run posture—PACKET-20 itself performs no RPA run.

## 13. CTO decisions and ED-11 register entries

Builder appends entries #261–#273 with the implementation PR:

- **#261 — module/model ownership.** Projection lives in
  `agents/projection_agent`, uses `claim_core.Base`, migration 0015, public
  builder/facade, and curated cross-package reads.
- **#262 — operation versus capability ids.** Operation ids remain
  `icon.*|edms.*`; canonical capabilities are `project.<operation>`. Provisional
  bare seeds are removed only after proving no durable evidence; otherwise
  startup blocks for owner migration.
- **#263 — operation availability schema.** Exact 15-row catalogue with
  `live|pending_capture|blocked_on_inputs`; future PRD-11/12 rows are registered
  but non-executable, and production paths are never fabricated.
- **#264 — hot-swap/pinning.** Claim semantic values/rules retain the claim's
  pack pin; target operation definition version and mode are operational
  configuration captured into each immutable projection snapshot. A mode/path
  change affects new rows only. A blocked request has no invalid row; event
  backfill creates it after capture.
- **#265 — screen and target-encoding gap.** Click paths lacked screen grouping,
  labels, and target money units required by §9.3. Add validated screen and
  paste metadata to the same per-operation YAML; missing metadata blocks.
- **#266 — projection PII.** Snapshot values cannot become a plaintext PII
  shadow store. Reuse claim-DEK envelopes and curated claim-core snapshot/decrypt
  reads; never duplicate key logic.
- **#267 — idempotency canonicalisation.** Snapshot hash and
  `<claim>:<operation>:<hash>` key are fixed as in §5; field version and
  operation-definition version are intentionally material.
- **#268 — reserve event compatibility and trigger gap.** The exact earlier
  PACKET-17 event maps only to `icon.reserve_create` after durable-source
  verification. Other production triggers belong to PACKET-22; no request
  payload is guessed.
- **#269 — paste timing/evidence.** Explicit start owns the clock; group state
  and attestation live in `evidence`; final event carries only actor/time/duration
  and hashes. Reads do not silently start timers.
- **#270 — crash-safe readback/FSM ownership.** A verifying row resumes by
  recognising its exact append-only field version; claim-register completion
  owns `REPORT_RECEIVED→REGISTERED`, never `REGISTERED→RESERVED`.
- **#271 — paste sampling.** A pack-configured Monday 08:00 EAT weekly task
  reuses deterministic SHA-256 selection with projection id at 10%; review
  payload carries ids/hashes only and duplicate weekly scans are idempotent.
  PACKET-21 owns mismatch-to-divergence behaviour.
- **#272 — approval-pack artifact boundary.** Paste era keeps the existing
  officer upload for items 12/13. No PDF scrape/download or invented projection
  artifact event is added in PACKET-20.
- **#273 — money-adjacent projection ceilings.** Canonical `project.*`
  capability classification hard-codes reserve/general-payment/salvage paths
  below L4 and PRD-12 payment workflows at L2 until their gate; pack data may
  tighten, never widen.

## 14. Builder guardrails

- **No external executor:** no Adapter ABC, `.execute`, Playwright, browser
  session, screenshot, service account, Secrets Manager client, or target
  network call.
- **AR-2:** paste-assist is authenticated human work, not a hidden bypass for
  an agent write. PACKET-21 registers the first external executor at the gate.
- **Never guess:** uncaptured path/order/regex/unit/value map is a named blocker,
  not a placeholder with executable defaults.
- **Append-only:** external readback uses `ClaimService.write_fields`; exact
  resume detection prevents duplicate versions; human-verified fields remain
  protected.
- **Money:** integer cents in storage and snapshots; boundary conversion is
  declared, exact, and never float/rounded.
- **PII:** encrypted in projection payload, decrypted only through claim core,
  access-logged, absent from events/reviews/logs.
- **Closed review enum:** use existing `PASTE_READBACK_CHECK` and `EXCEPTION`
  subtypes only; no eighteenth type.
- **Single ledger writer:** event mapping only; no direct ledger write.
- **No payment execution:** future payment operations are blocked registry
  slots. No transfer, approval, release, or funds movement exists.
- **Portal isolation:** no portal/public endpoint or whitelist change.
- **Config over code:** operation mode, availability, definition version,
  screen order, labels, bindings, target encodings, validators, and blockers
  are pack data.
- **No protected regression rewrite:** prior fixtures remain semantically
  unchanged. A new Packet-20 suite may add protected coverage.

## 15. Definition of done and hand-off

- `ruff check .`, money lint, banned-call lint, full SQLite and PostgreSQL tiers
  green;
- backend pooled statement+branch coverage ≥80%; pack calculations stay 100%;
  frontend ≥70%;
- migration 0015 reviewed on both dialects;
- generated OpenAPI includes all five projection routes and does not expose a
  request-with-values or adapter route;
- projection runbook covers blocked configuration, exact snapshot inspection,
  PII access, stale group updates, start/confirm idempotency, verifying recovery,
  conflicting readback, cache dispatch, FSM mismatch, and sampled review;
- grader map registers projection-readback fields to existing critical
  `G-VAL`; the eval consumer grades `source_type=projection_readback` external
  fields against the captured validator before that output counts as autonomy
  evidence—no tenth grader id is invented;
- PR description lists register #261–#273, exact files changed, migration,
  OpenAPI diff, coverage, protected tests, and live blockers;
- production motor operation paths remain honest `pending_capture`/
  `blocked_on_inputs`;
- no adapter health is claimed, no RPA acceptance is claimed, and no external
  write occurs.

Builder order:

1. model + migration + PostgreSQL fixture registration;
2. operation/capability catalogue and constitution migration;
3. curated claim-field snapshot/PII boundary;
4. request consumer + idempotent row creation;
5. click-path/field-strip parser;
6. paste lifecycle + readback + FSM;
7. sampling + authenticated APIs;
8. Claim-360 Systems workspace;
9. ledger map, OpenAPI, runbook, coverage, full regression.

PACKET-21 consumes unchanged:

- exact projection DDL and status vocabulary;
- operation registry + captured operation version;
- canonical `project.<operation>` capability ids;
- immutable snapshot/hash/idempotency semantics;
- paste evidence/readback shape;
- projection API/list surface;
- existing projection event ids and ledger mappings.

Stop and append a new ED-11 register entry before making any choice not fixed
above. Do not solve PACKET-21/22 or a live discovery dependency in this packet.
