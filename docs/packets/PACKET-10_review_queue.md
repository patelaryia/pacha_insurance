# PACKET-10 — Review-queue substrate & resolution engine (PRD-04 slice 1 of 3)

> **Status:** issued · **Builder:** Codex per `AGENTS.md` · **Reviewer:** CTO per `CLAUDE.md`
> **Source spec:** `docs/PRD-04_Status_Console_and_Review_Queue_v1.1.md`
> §4.2 (RBAC), §4.3 (S-1 enum + four-part contract + routing + resolution),
> §4.5 acceptance scenarios 4 and 6; PRD-03 §3.5; PRD-00 §0.2/§0.4;
> Section 0 ED-8/ED-11; Section 0.5 AR-2.
> Precedence: Section 0 → Section 0.5 → PRD-04/PRD-03/PRD-00 → this packet.
> **Depends on:** PACKET-09 merged on main (74caf57), including its CTO
> close-out (registers #87/#88).
> **Acceptance tests:** `tests/acceptance/test_packet_10_review_queue.py` —
> protected, failing by design until this packet is built.
> **Packet 11 (next):** PRD-04 slice 2 — console SPA S-1/S-2, Entra ID SSO/RBAC,
> pdf.js citation viewer, keyboard rules (scenarios 1/3/5).
> **Packet 12:** PRD-04 slice 3 — S-3–S-6, notifications/websocket/digest
> (AR-5 `notify/`), portfolio series windows (register #79), SLA board.

## 0. PRD-04 slice map (CTO cut)

| Slice | Packet | Scope | Why this boundary |
| --- | --- | --- | --- |
| 1 | **PACKET-10 (this)** | `review_items` projection, closed 17-type enum, four-part contract registry as pack data, versioned resolution schemas, resolution engine + API, server-side RBAC/bands, typed side-effect wiring to existing engine methods | Every earlier packet already *produces* `review.created` events and PRD-03 already *consumes* `review.resolved`; the server substrate closes that loop and is fully testable without a browser |
| 2 | PACKET-11 | React SPA S-1 + S-2, Entra ID SSO replacing `X-Actor` (register #20), citation viewer, keyboard focus rules | UI binds to the slice-1 API unchanged; SSO swaps the identity layer only |
| 3 | PACKET-12 | S-3–S-6, AR-5 notifications, websocket, digest, dashboard series (register #79), SLA board | Needs slice-1 rows + slice-2 shell; series windows are an open register item |

## 1. Scope

**In:**

1. **`review_items` table** (new, on `claim_core` Base, migration included).
   Local minimal DDL (PRD-04 defines behaviour, not columns — register #89):
   `id` ULID PK; `claim_id` nullable text (promotion sign-offs are claim-less);
   `type` text constrained to the closed 17-type enum
   (`cop_runtime.contracts.REVIEW_ITEM_TYPES` is the single source);
   `subtype` nullable text; `status` `'open'|'resolved'|'cancelled'`;
   `payload` JSON (verbatim producing-event payload: agent output + citations);
   `source_event_id` text unique (idempotency key); `assigned_to` nullable text;
   `created_at`; `resolved_at` nullable; `resolved_by` nullable;
   `resolution` nullable text; `resolution_payload` nullable JSON;
   `resolution_schema_version` nullable text.
2. **Projection consumer `review_queue`** on the existing dispatcher:
   `review.created` → one row, idempotent on `source_event_id`, synchronously
   drivable with `dispatch_once()`. `queue.backfill(actor)` idempotently
   replays historical `review.created` events (decline approvals from register
   #23, EXCEPTIONs, SAMPLE_REVIEWs, DOC_SPLITs) into rows. An event whose
   `type` is not one of the 17 is projected as `EXCEPTION` with
   `subtype="unknown_review_type"` and the original payload preserved
   verbatim — never dropped, never added to the enum. `assigned_to` copies the
   claim's `claims.assigned_to` at projection time.
3. **Four-part contract registry as pack data** —
   `packs/motor/review/contracts.yaml` + `packs/motor/review/schemas/`:
   for each of the 17 types: (1) `producing_events` (non-empty list),
   (2) `workspace_layout` id (data only; console binds it in PACKET-11 —
   register #90), (3) `resolution_actions` — exactly
   `[approve, edit_approve, reject]` (PRD-04 §4.3), (4) `resolution_schema`
   ref `<TYPE>@1` resolving to `schemas/<TYPE>@1.json` (versioned like
   prompts). Per-type authorisation is also data: `authorised_roles`
   (non-empty list) and, for band-gated types (`PACK_REVIEW`, `EX_GRATIA`),
   `band_amount_path` (motor: `assessment.agreed_quote`). Loading a contracts
   file with an 18th type, a missing part, or a missing schema file raises
   `ValueError` — fail closed.
4. **Versioned resolution schemas (supersede register #72 v0).** Common core
   required by every v1 schema:
   `{capability_id: string, diff: {typed_changes: [{path, kind}...],
   prose_change_ratio: number}}`; type extras: `reason` (string, required on
   reject — enum list `pending_capture`, register #91), `corrected_fields`
   (FIELD_VERIFY edit_approve), `boundaries` (DOC_SPLIT). The resolver — not
   the client — derives `resolution` from the action
   (`approve→approved`, `edit_approve→edited`, `reject→rejected`, register #80
   spelling) and composes the `review.resolved` event payload as the validated
   request payload plus `{review_id, type, schema_version, resolution}`, so
   the PRD-03 autonomy counters and PACKET-09 correction capture keep reading
   `capability_id`/`resolution`/`diff.typed_changes` unchanged (register #93).
5. **Resolution engine + API.**
   `GET /reviews?scope=mine|pool&type=&status=&claim_id=` (`mine` = items on
   claims where `claims.assigned_to` = actor, `pool` = all; response
   `{"items": [...]}` with at least `id, claim_id, type, subtype, status,
   assigned_to, payload, sla`); `GET /reviews/{id}`;
   `POST /reviews/{id}/resolve`. Resolve, in **one transaction**: item must be
   `open` (else 409 `ALREADY_RESOLVED`); actor authorised (below); schema
   version known (else 422 `SCHEMA_VERSION_UNKNOWN`) and payload valid (else
   422 `PAYLOAD_INVALID`); writes the row's resolved fields **and** the
   `review.resolved` event. Resolution always logs the **actual** actor even
   when the item was default-routed to someone else (PRD-04 §4.3 assignment
   model). `review.resolved` and denials are ledgered through the existing
   single-writer consumer via the ACTION_MAP additions (register #94).
6. **Server-side RBAC (interim identity).** Roles per PRD-04 §4.2 from org
   config `packs/motor/routing/roles.yaml` mapping `user:<ULID>` → role;
   identity remains the mandatory `X-Actor` header until PACKET-11 SSO
   (extends register #20 — register #92). `build_review_queue(app, roles=...)`
   accepts an injected mapping for tests; `None` loads the org file.
   Authorisation, in order: unmapped `user:*` or any `agent:*`/`system` actor
   → 403 `FORBIDDEN_ROLE` (resolve is human-only); role not in the type's
   `authorised_roles` → 403 `FORBIDDEN_ROLE`; band-gated type → resolve the
   claim's current `band_amount_path` money value against
   `packs/motor/routing/authority_matrix.yaml`, **inclusive at `max`**
   (boundary test at exactly `100_000_00`); amount above the actor's band →
   403 `FORBIDDEN_BAND`; band amount missing on the claim → 409
   `RESOLUTION_BLOCKED_ON_INPUTS` (never guess). Every 403 emits an
   `authz.denied` event → audit-ledger row (acceptance scenario 4).
   `EXCEPTION{decline_approval_required}` requires role `claims_manager`
   (PRD-00 §0.4). `auditor` is read-only everywhere. `head_of_claims` has
   **no approval band by default** (open item 13 — config slot, not code).
7. **Typed side effects, strictly through existing engine methods** — the
   resolver adds no new write path:
   - `FIELD_VERIFY` — `corrected_fields` are written through the claim
     service's human `human_verified` write (append-only versioning; the
     agent-supersession 409 `HUMAN_OVERRIDE_PROTECTED` of PRD-00 §0.2 is
     untouched).
   - `DOC_SPLIT` — `edit_approve` with `boundaries` calls
     `app.state.doc_intel.apply_human_boundaries(document_id, boundaries=...,
     actor=<actual actor>)` (register #45 transport). Engine absent →
     409 `RESOLUTION_BLOCKED_ON_INPUTS`, item stays open.
   - `EXCEPTION{decline_approval_required}` — approve commits the blocked
     decline via the authorised FSM extension (register #95); reject leaves
     the claim unchanged and clears the pending-approval blocked reason.
   - `PROMOTION_SIGNOFF` — records the resolution only; the level change
     itself still happens exclusively through the PACKET-08 promotion API with
     its `sign_offs` validation (approval authority is not a capability,
     guide §3.11). Resolving never mutates `capabilities`.
   - `NOTE_REVIEW` / `PACK_REVIEW` / `DRAFT_RELEASE` — resolution emits the
     `review.resolved` decision only; rendering/sending stays with its
     producer and release remains human-only per the guide §3.11 ceilings.
   - All other types — generic resolve (row + event, no side effect); their
     producers arrive with later PRDs.
8. **SLA chip data.** `GET /reviews` items carry `sla`: the claim's
   `sla_clocks` rows (existing PRD-00 engine) with at least
   `definition_id` and `state`; no new SLA machinery, no new definitions.
9. **Scenario-6 integration.** A reject-with-reason resolution on a
   capability-tagged item round-trips through the PACKET-09
   `correction_capture` consumer into a complete `production_correction` test
   case (PRD-04 §4.5 (6), PRD-03 §3.5) — asserted end-to-end in acceptance.

**Out / blocked:** all six screens' UI, React SPA, Entra ID SSO, keyboard
rules, pdf.js citation viewer (scenarios 1/2/3/5 and every §4.5 NFR timing);
websocket/email notifications and digest (AR-5 `notify/`); portfolio tiles and
series windows (register #79); SLA board bulk escalate; S-6 admin screens;
role administration UI; reject-reason enum values (item capture); HOC band
decision (open item 13); officer assignment logic (`claims.assigned_to` is
written by PRD-05 §5.8 — this packet only reads it).

## 2. Binding spec quotes (implement verbatim)

PRD-04 §4.3:

> "**Review-item type enum — FINAL AND CLOSED (v1.1). Do not add types; new
> cases are `EXCEPTION` subtypes:** `FIELD_VERIFY, DOC_CLASSIFY, DOC_SPLIT,
> CONSISTENCY_FLAG, DRAFT_RELEASE, MODE_CONFIRM, NOTE_REVIEW, PACK_REVIEW,
> EX_GRATIA, EXCEPTION, PROMOTION_SIGNOFF, SAMPLE_REVIEW,
> PASTE_READBACK_CHECK, PROCEED_PARTIAL, KYC_VERIFY, EFT_MATCH, REOPEN_PROMPT`"

> "**Four-part contract (mandatory for every type):** each type ships with
> (1) producing event(s), (2) workspace layout, (3) resolution actions, (4) a
> **versioned resolution-payload JSON schema** — the payload schemas are
> consumed as training data by PRD-03 §3.5, so schema changes are versioned
> like prompts."

> "items default-route to the claim's owning officer (`claims.assigned_to`
> ...); an "all items" pool view lets any officer work anything — resolution
> always logs the actual actor. ... exactly three primary actions:
> **Approve / Edit→Approve / Reject (reason required, enum + free text)**."

> "Resolution writes `review.resolved` with structured diff (feeds 3.5)."

PRD-04 §4.2:

> "A user's approval band comes from role; the console never lets anyone
> approve outside band (server 403)."

PRD-04 §4.5:

> "(4) approval attempted outside band → 403 + ledger entry; ...
> (6) reject-with-reason round-trips into a production_correction test case."

PRD-00 §0.4 (via register #23):

> post-triage decline "requires a claims_manager approval item before the
> transition commits"

## 3. Deliverable

```text
platform/review_queue/
  __init__.py       # build_review_queue(app, *, roles=None, contracts_path=None)
  models.py         # review_items on claim_core Base (per-dialect JSON)
  projection.py     # review_queue consumer + idempotent backfill
  contracts.py      # pack contract/schema loader; closed-enum enforcement
  service.py        # queue reads, resolution transaction, side-effect wiring
  rbac.py           # role/band authorisation from org+pack config
  api.py            # GET /reviews, GET /reviews/{id}, POST /reviews/{id}/resolve
packs/motor/review/contracts.yaml
packs/motor/review/schemas/<TYPE>@1.json   # 17 files, v0-superset core
packs/motor/routing/roles.yaml             # user→role org config; HOC bandless
platform/claim_core/alembic/versions/000X_review_items.py
```

Cross-package access via `claim_core` root exports and `app.state` only
(ED-1). **Authorised `claim_core` changes — exactly three** (registers
#94/#95), nothing else:

1. `ledger.ACTION_MAP` gains `review.resolved` → `review.resolved` and
   `authz.denied` → `authz.denied` (PACKET-03 §4.4 anticipated the map
   growing; PACKET-08 register #70 precedent).
2. The FSM decline path gains an explicit approval context
   (`approved_by_event: str | None`): only when set does a decline commit
   from `DECLINE_APPROVAL_STATES`, and the transition event payload records
   `approved_by_event` (register #23 wiring, register #95).
3. `_blocked_reasons` treats `decline_approval_required` as pending only
   while its review item is unresolved (a matching `review.resolved` event
   clears it) — a rejected decline request must not block the claim forever.

Side effects call only already-public engine methods (§1.7). `.github/`,
`tools/ci/`, `pyproject.toml`, and protected acceptance files untouched by
the builder.

### 3.1 Pinned public surface (acceptance relies on exactly this)

```python
from review_queue import build_review_queue

queue = build_review_queue(app, roles=None, contracts_path=None)
#   roles: dict "user:<ULID>" -> role, None loads packs/motor/routing/roles.yaml
#   contracts_path: review/ pack dir override, None loads packs/motor/review/
queue.backfill(actor="system")           # idempotent event-history replay

# GET  /reviews?scope=mine|pool&type=&status=&claim_id=   (X-Actor mandatory)
#   -> {"items": [{id, claim_id, type, subtype, status, assigned_to,
#                  payload, sla: [{definition_id, state, ...}]}]}
# GET  /reviews/{id}
# POST /reviews/{id}/resolve
#   {"action": "approve"|"edit_approve"|"reject",
#    "schema_version": "<TYPE>@1",
#    "payload": {capability_id, diff{typed_changes[], prose_change_ratio},
#                + type extras (reason / corrected_fields / boundaries)}}
# -> 200 resolved item
#  | 403 {"code": "FORBIDDEN_ROLE"|"FORBIDDEN_BAND"}   (+ authz.denied ledgered)
#  | 409 {"code": "ALREADY_RESOLVED"|"RESOLUTION_BLOCKED_ON_INPUTS"}
#  | 422 {"code": "SCHEMA_VERSION_UNKNOWN"|"PAYLOAD_INVALID"}
```

## 4. CTO decisions (D-x) and register entries

- **#89 — `review_items` DDL is undefined by PRD-04.** Minimal event-sourced
  projection DDL per §1.1; the table is a *projection* of the event spine
  (rebuildable via backfill), so columns may be extended by later packets only
  with a register entry.
- **#90 — workspace layouts are UI scope.** The contract registry stores
  layout *ids* as data; PACKET-11 binds them to React components. Shipping the
  id satisfies part (2) of the four-part contract for this slice.
- **#91 — reject-reason enum values are uncaptured.** Free-text reason is
  mandatory now; the pack enum list ships `pending_capture` and membership is
  enforced only once the CM supplies values. Never invent reason codes.
- **#92 — identity before SSO.** `X-Actor` + `roles.yaml` org config carries
  authorisation until PACKET-11 Entra ID (extends #20). Role claims are
  config, not self-asserted headers; an unmapped `user:*` actor is 403.
- **#93 — resolution schema supersession.** v1 schemas are strict supersets of
  the #72 interim v0; PRD-03 consumers must keep passing unmodified. Any
  breaking schema change is a version bump plus consumer migration, like
  prompts.
- **#94 — 403s and resolutions must reach the ledger** but no event type maps
  them. `authz.denied` + `review.resolved` join `ACTION_MAP`; single-writer
  ledger consumer unchanged.
- **#95 — PRD-00 §0.4 decline approval has no commit transport** (register
  #23 deferred it here). The FSM gains the explicit `approved_by_event`
  context set only by the resolver from a `claims_manager`-authorised
  resolution; `_blocked_reasons` becomes resolution-aware so a rejected
  decline stops blocking.

## 5. Builder guardrails

- **Closed enum stays at 17** — reject the 18th type at pack load *and* at
  projection; new cases are `EXCEPTION` subtypes (PRD-04 §4.3).
- **Append-only writes** — FIELD_VERIFY corrections go through the existing
  claim-service human write; no in-place `claim_fields` update; agents never
  supersede `human_verified` (409 unchanged).
- **Never guess** — unknown schema version, unmapped actor, uncaptured reason
  enum, missing band amount, unbuilt side-effect target: all resolve to
  4xx/`RESOLUTION_BLOCKED_ON_INPUTS` visible states, never a default.
- **No payment execution, no new adapter ops** — EFT_MATCH/KYC_VERIFY are
  generic resolves in this slice; GP-1 posture unchanged.
- **Single-writer ledger** — denials and resolutions are ledgered via the
  existing event→ledger consumer only; no direct `audit_ledger` writes.
- **Autonomy ceilings untouched** — PROMOTION_SIGNOFF resolution never writes
  `capabilities`/`autonomy_changes`; release actions stay human-only.
- Bands/roles/reasons/schemas/layout ids are pack or org **config data**,
  never literals; band comparison is integer KES cents, inclusive at `max`.
- Resolution API is human-only; `agent:*`/`system` actors 403 on resolve.
- All PACKET-01–09 suites keep passing unmodified.

## 6. Definition of done (ED-7/ED-7a)

- All tests in `tests/acceptance/test_packet_10_review_queue.py` pass
  unmodified; full suite green on SQLite and PostgreSQL legs.
- Acceptance scenarios 4 and 6 implemented **verbatim**, including the band
  boundary at exactly `100_000_00` (inclusive) and the agent-resolve 403.
- Unit coverage: enum closure (pack load + projection), idempotent
  projection/backfill, double-resolve 409, schema version pinning + v0
  superset property, per-type side-effect wiring incl.
  `RESOLUTION_BLOCKED_ON_INPUTS`, unmapped-actor 403 ledgering, decline
  approve/reject both paths.
- ≥80% coverage on `platform/review_queue/`; ruff, money-float lint,
  banned-calls, pytest green; migration reviewed; OpenAPI generated; runbook
  content (projection lag, blocked resolutions, denial triage) flagged in the
  PR description (CTO owns protected docs).
- Grader coverage: no new OutputType in this slice; `grader_map.yaml`
  unchanged — confirm explicitly in the PR description.
- ED-11: any further ambiguity ⇒ narrowest safe behaviour + proposed register
  entry; stop and flag before expanding this packet.
