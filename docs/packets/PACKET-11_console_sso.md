# PACKET-11 — Review console, Claim 360, Entra SSO & citation viewer (PRD-04 slice 2 of 3)

> **Status:** issued · **Builder:** Codex per `AGENTS.md` · **Reviewer:** CTO per `CLAUDE.md`
> **Source spec:** `docs/PRD-04_Status_Console_and_Review_Queue_v1.1.md`
> §4.1, §4.2, §4.3 S-1/S-2, §4.5 scenarios 3 and 5; Section 0
> ED-1/ED-2/ED-6/ED-8/ED-11; PRD-01 §1.4; PACKET-10 public queue contract.
> Precedence: Section 0 → PRD-04/PRD-01/PRD-00 → PACKET-10 → this packet.
> **Depends on:** PACKET-10 merged on main (`ea02062`), including CTO close-out
> registers #96–#98.
> **Acceptance:** `tests/acceptance/test_packet_11_console_api.py` and
> `tests/acceptance/console/test_packet_11_console.test.tsx` — protected and
> failing by design until this packet is built.
> **Packet 12 (next):** PRD-04 slice 3 — S-3–S-6, websocket/email/digest via
> AR-5 `notify/`, portfolio series windows (register #79), and the SLA board.

## 0. Slice boundary

PACKET-10 made review items durable and resolvable. This packet gives staff the
first browser surface over that API and replaces the temporary human identity
transport at the console ingress. It does not pull Phase-2 producer logic or
PACKET-12 dashboards forward.

**In:** React S-1 and S-2; all 17 workspace-layout bindings; FIELD_VERIFY's
complete citation/verify/correct path; exact keyboard rules; Entra access-token
verification and immutable identity mapping; a read-only Claim-360 aggregate;
secure normalised-PDF delivery; browser and backend acceptance; frontend CI.

**Out / visibly unavailable:** S-3–S-6; role/user administration; websocket,
email and digest; checklist/chase detail (PRD-06); projection-system detail
(PRD-09); threaded communications producers (PRD-05/06); approval-pack inline
viewer (PRD-08); reopen execution (PRD-05); live tenant secrets; production
identity rows; scenario 1's full Phase-2 synthetic-claim run; scenario 2's
portfolio surface; scenario 3's human timing result. Empty producer-owned tabs
render an explicit `Not available until <PRD> is installed` state — never
fabricated data and never a silent blank.

## 1. Entra identity boundary

### 1.1 Operational contract

Add `review_queue.install_console(app, *, verifier=None, identities=None,
roles=None)`. Installation happens after `build_review_queue`; it:

1. protects `/auth/*`, `/reviews*`, and `/console/*` with a console-ingress
   middleware;
2. rejects a network `X-Actor` header with `400 ACTOR_HEADER_FORBIDDEN`;
3. requires `Authorization: Bearer <access-token>` (`401
   AUTHENTICATION_REQUIRED` / `INVALID_TOKEN`);
4. validates signature, algorithm, issuer, audience, tenant, expiry and
   not-before using Entra OIDC metadata/JWKS; metadata/JWKS may be cached but an
   unknown `kid` triggers one bounded refresh, never token acceptance without
   verification;
5. reads only immutable `tid` + `oid` as the external identity, maps that exact
   pair through organisation config to one internal `user:<ULID>`, then injects
   that actor into the existing app internally;
6. resolves the role independently from `roles.yaml`; token roles/groups,
   names, email addresses and client-supplied actor values never grant access;
7. returns `403 IDENTITY_NOT_MAPPED` for an otherwise valid but unmapped pair
   and `403 FORBIDDEN_ROLE` when the actor has no configured role.

The browser uses MSAL's authorization-code + PKCE flow. Token storage is memory
or session storage only; never local storage. Tenant id, client id, API audience,
scope, redirect URI and authority are deployment configuration, validated at
startup and absent from committed production values.

`GET /auth/me` returns exactly `{actor, role}`. It does not echo token claims,
OID, tenant id or bearer material.

### 1.2 Test seam and config

```python
from review_queue import install_console
from review_queue.auth import TokenClaims, TokenVerifier

console = install_console(
    app,
    verifier=fake_verifier,  # TokenVerifier.verify(token) -> TokenClaims(tid, oid)
    identities={"<tenant-id>:<object-id>": "user:<ULID>"},
    roles={"user:<ULID>": "claims_officer"},
)
```

`None` loads deployment settings plus `packs/motor/routing/identities.yaml` and
the existing `roles.yaml`. The committed identity file contains schema/version
and an empty mapping only. Duplicate external identities, duplicate actor
targets, malformed actors, missing config, or actor-without-role fail startup.
Tests inject a verifier and make no network call.

The middleware is the compatibility membrane: prior packets may continue to
exercise uninstalled test apps with `X-Actor`; once `install_console` is called,
no human console request can assert its own actor. Machine-to-machine ingress is
an infra/auth packet concern; agents continue to call public in-process engine
interfaces in this slice.

## 2. Console read API

All endpoints use only the authenticated actor and server-side role checks.
Allowed S-1/S-2 roles are `claims_officer`, `asst_claims_manager`,
`claims_manager`, `gm`, `md`, `chairman`, `head_of_claims`, and read-only
`auditor`. `finance` and `admin` have no S-1/S-2 route in this slice and receive
403. Resolution keeps PACKET-10's per-type roles/bands; auditor remains read-only.

### 2.1 Claim 360

`GET /console/claims/{claim_id}/360` returns one coherent read model from the
existing public claim-core reads:

```text
claim       id, status, substatus, assigned_to, created_at, updated_at
header      insured, registration, amount (nullable, never guessed)
fields      path, value, value_type, verification_state, confidence,
            source_type, has_citation
documents   current PRD-00 document metadata
financials  present money fields only, cents encoded as decimal strings,
            plus calc_run_id when source_ref supplies it
timeline    current event stream, EAT rendering remains a UI concern
systems     projection.* timeline facts only
communications email.* timeline facts only
availability {document_checklist, systems, communications} status + owner PRD
```

Header paths are pinned: `parties.insured.name`, `vehicle.reg`, and routing
amount `settlement.payable` then `reserve.total` fallback. Missing fields are
`null`; no other path is substituted. `DECLINED` is status, not a presentation
hint: the UI must render the full-width banner described in §4.3.

The API introduces no table, column or migration. It does not query another
package's tables directly: claim, documents and timeline come through
`app.state.claim_service`; review data through `app.state.review_queue`.

### 2.2 Citation transport

`GET /console/claims/{claim_id}/fields/{field_path}/citation` returns:

```json
{
  "claim_id": "...", "field_path": "vehicle.reg", "value": "KAA 111A",
  "value_type": "string", "verification_state": "human_verified",
  "document_id": "...", "page": 1, "bbox": [0.1, 0.2, 0.4, 0.3],
  "document_url": "/console/documents/.../normalised.pdf"
}
```

The `bbox` is the exact stored, normalised `[x0,y0,x1,y1]`. The resolver walks
the field's append-only versions newest-to-oldest and selects the newest valid
same-claim extraction citation; this preserves evidence after a human-verified
correction while the value panel shows the current value. It never re-matches
anchor text and never synthesises a box. No valid citation, wrong-claim
document, invalid box/page, absent PDF, or underived field ⇒ `409
CITATION_UNAVAILABLE`.

`GET /console/documents/{document_id}/normalised.pdf` returns only the existing
`normalised/{document_id}.pdf`, `Content-Type: application/pdf`,
`Cache-Control: private, no-store`, and `X-Content-Type-Options: nosniff` after
the same role/claim access check. Original uploads and arbitrary blob keys are
never addressable.

## 3. React application

Create the ED-2 frontend in `console/`: React 18, TypeScript strict mode, Vite,
TanStack Query, Tailwind, MSAL, pdf.js, React Router, Vitest, Testing Library,
axe, and coverage. Commit `package-lock.json`; exact dependency versions are
lockfile-pinned. `npm run build`, `npm run lint`, and `npm run test:coverage`
are non-interactive and CI-safe.

### 3.1 App shell and data rules

- Routes: `/queue`, `/claims/:claimId`; `/` redirects to `/queue` after auth.
- The API client attaches only the bearer token. It never sends `X-Actor`.
- All money is `bigint` KES cents in the TypeScript domain. JSON is parsed and
  serialised losslessly; display uses integer division and shows fractional
  cents only when non-zero. No JavaScript `number` represents money.
- UTC timestamps render in `Africa/Nairobi`; generated prose uses British
  English.
- Loading, empty, 401/403, retryable read failure, blocked input and mutation
  failure states are visible. A failed resolution remains selected and is not
  optimistically removed.
- Layout works at 1366×768 without horizontal page scroll; Chrome and Edge are
  the supported browsers. Queue/360 API durations are captured in tests and
  remain subject to the PRD p95 production gate.

### 3.2 S-1 Review Queue

Left pane: Mine/Pool selector, type/status filters, SLA chip, assigned officer,
claim id and item type. Selection uses roving `tabIndex` with one focused row.
Right pane dispatches every configured `workspace_layout`; an unknown layout
renders a blocking `Unsupported workspace` state and disables resolution.
All 17 ids have an explicit component binding. Generic layouts may share a
component, but no id may fall through silently.

PACKET-10's `GET /reviews` and `GET /reviews/{id}` responses gain
`workspace_layout` and `resolution_schema`, joined from the server-loaded
contract registry. The browser never derives either from the type name and
never pins an independent schema-version map.

Every workspace shows the producing payload, citations when present, and
exactly three primary actions labelled `Approve`, `Edit→Approve`, `Reject`.
Reject requires free text while the reason enum remains visibly
`pending_capture` per register #91. The client sends the contract's pinned
`<TYPE>@1` schema version and structured diff. Server 4xx error codes are shown
verbatim enough for an officer to act.

Keyboard behaviour is binding:

- handlers live on the list/item container — never `window`/`document`;
- `j/k` moves the roving focus; `a/e/r` only act on an explicitly focused item;
- all shortcuts are inert while focus is inside input, textarea, select or
  contenteditable; `Esc` leaves editing/dialog state and returns focus to the
  selected list row;
- normal modified shortcuts (`Alt`, `Ctrl`, `Meta`) are never intercepted.

### 3.3 S-2 Claim 360 and FIELD_VERIFY

Header shows claim id, insured, registration, amount and a horizontal FSM rail.
All 24 canonical states are represented. `DECLINED` additionally renders a
full-width red banner with a visible Reopen action; until PRD-05 ships, clicking
it opens the explicit `Reopen unavailable — PRD-05` state and performs no API
mutation.

Tabs are exactly `Overview`, `Documents`, `Fields & Citations`, `Financials`,
`Timeline`, `Systems`, `Communications`. Producer-owned unavailable data uses
the availability markers from §2.1.

The Fields table opens the citation viewer. pdf.js renders the normalised PDF;
the overlay is computed from the rendered page viewport and the exact
normalised bbox, survives scale/rotation, scrolls the cited page into view, and
has an accessible non-colour-only label. FIELD_VERIFY places current/candidate
value beside it and supports verify or correct inline via PACKET-10 resolve.

## 4. Protected acceptance contract

Backend acceptance proves: raw/spoofed actor rejection; valid/unmapped/invalid
token paths; exact `(tid,oid)` mapping and independent role lookup; `/auth/me`;
actor attribution on resolution; S-2 role denial; coherent 360 fields; money as
decimal cents strings; historical citation resolution; wrong-claim/no-render
refusal; secure PDF headers; and no network with the fake verifier.

Frontend acceptance proves: bearer-only API; mine/pool/filter behaviour;
exactly three actions; 17 layout bindings and fail-closed unknown layouts;
roving focus and input suppression; Escape focus return; DECLINED banner + all
seven tabs; bigint money formatting; server-error persistence; and 50 exact bbox
overlays. Vitest statements/branches/functions/lines are each ≥70% across
`console/src`.

The following remain live acceptance, not synthetic claims:

- scenario 3: a human processes 20 FIELD_VERIFY items; median handle time ≤10s;
- scenario 5 final: 50 sampled production fields, zero visual highlight misses;
- queue and 360 p95 <400ms; viewer page <1.5s on the target desktop/network.

The protected browser test proves the 50-box coordinate transform mechanically;
it does not falsely discharge the production sampling or human timing gates.

## 5. Deliverable and authorised changes

```text
platform/review_queue/
  auth.py             # TokenVerifier, Entra verifier, identity map, middleware
  console_api.py      # /auth/me, Claim-360 and citation/PDF routes
  console_reads.py    # read aggregation and append-only citation lineage
  __init__.py         # install_console public export
packs/motor/routing/identities.yaml
console/
  package.json package-lock.json tsconfig*.json vite.config.ts
  src/auth/ src/api/ src/components/ src/pages/ src/workspaces/ src/lib/
  src/**/*.test.tsx
docs/runbooks/status_console.md
requirements.txt       # maintained OIDC/JWT validation dependency; no hand-rolled crypto
```

Authorised existing-package changes are limited to:

1. `review_queue.__init__` exposes `install_console`; the existing
   `build_review_queue` signature remains compatible.
2. `ReviewQueue` exposes read-only `roles`/contract metadata needed by the
   console, and review reads include `workspace_layout`/`resolution_schema`;
   resolution semantics and the closed 17-type set do not change.
3. `claim_core` may add one curated read method for append-only field-version
   citation lineage. No write path, model, DDL or migration change is allowed.
4. Frontend dependency/tool configuration and this packet's CI steps.

No new event type, adapter operation, capability, review type, table, column,
or funds-transfer path. No direct `audit_ledger` write. No changes to prior
protected acceptance files.

## 6. CTO decisions / open-register entries

- **#99 — Entra claim mapping is unspecified.** Exact immutable `(tid,oid)` →
  internal ULID actor in deployment config; role remains independent org data;
  every other claim is non-authoritative.
- **#100 — replacing `X-Actor` without breaking in-process agents.** Installed
  console ingress rejects the network header and internally supplies the
  verified actor to legacy route handlers. Uninstalled packet test apps keep
  their prior transport; machine ingress auth is deferred to infra.
- **#101 — S-2 producer data is not built.** Aggregate only committed fields,
  documents and events; checklist/projection/communication detail carries an
  explicit producer-owned unavailable state.
- **#102 — citations after human correction.** Use the newest valid historical
  extraction citation from append-only versions and current value separately;
  never rematch or invent evidence.
- **#103 — Claim-360 routing amount/header bindings are not stated.** Pin the
  doc-set's `parties.insured.name`, `vehicle.reg`, and C-08 routing amount with
  `reserve.total` fallback; missing stays null.
- **#104 — PRD scenarios mix CI and human/live measures.** Mechanical browser
  tests cover interaction and 50 exact transforms; human timing, sampled visual
  misses and production p95 remain explicit trial gates.
- **#105 — S-1/S-2 role rows are incomplete.** Permit only operational claims
  roles + HOC + read-only auditor; finance/admin wait for their named S-3/S-6
  surfaces. Resolution still uses the narrower per-type contract/band.

## 7. Builder guardrails and definition of done

- Entra verification fails closed; no unsigned decode, wildcard issuer/audience,
  token-derived role, committed tenant secret, or raw bearer logging.
- No browser request carries `X-Actor`; no identity is inferred from email/name.
- Citation documents must belong to the field's claim; arbitrary blob access is
  impossible; missing evidence is `CITATION_UNAVAILABLE`.
- Closed review enum remains exactly 17; all layout ids explicit; unknown ids
  block actions.
- FIELD_VERIFY corrections still use PACKET-10's append-only human write; no
  in-place field update and no client-side success before server success.
- Money remains integer cents end-to-end and `bigint` in the browser domain.
- `ruff check .`, money-float lint, banned-calls, full pytest (SQLite + PG),
  frontend lint/build/Vitest all green. Prior packet suites stay unmodified.
- Backend changed-package coverage ≥80%; frontend Vitest ≥70% for statements,
  branches, functions and lines. No migration. OpenAPI includes the new routes.
- Runbook covers Entra/JWKS/config failure, unmapped identity, 401/403 triage,
  citation refusal, PDF/blob safety, frontend rollback and live timing protocol.
- Any further ambiguity: narrow fail-closed behaviour + proposed register entry;
  stop before expanding the packet.
