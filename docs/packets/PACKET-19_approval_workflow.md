# PACKET-19 — Approval-note review, crash-safe signing, authority routing, and S-3 approval (PRD-08 slice 2 of 2)

> **Status:** issued · **Builder:** Codex per `AGENTS.md` · **Reviewer:** CTO per
> `CLAUDE.md`
>
> **Source spec:** `docs/PRD-08_Approval_Pack_Generator_v1.1.md` §8.2,
> §8.5–§8.7; PRD-00 §0.4; PRD-02 §2.4–§2.5; PRD-03 §3.3/§3.5; PRD-04
> §4.2–§4.5; PRD-09 §9.3 seam only; Section 0.5 AR-1/AR-2/AR-4/AR-5;
> Section 0 ED-1/ED-6a/ED-7/ED-8/ED-11; guide §3/§4/§6; registers
> #5/#6/#30/#61/#62/#67/#91/#104/#108/#116/#130/#157/#219–#245.
>
> **Depends on:** PACKET-18 merged and green.
>
> **Acceptance:** new protected backend and console Packet-19 suites. The
> builder does not weaken or rewrite Packet-01–18 acceptance fixtures.
>
> **Next packet:** PRD-09 paste-assist projection substrate. No PRD-09 adapter,
> click-path, reconciliation, or external-system write belongs here.

## 0. CTO disposition and slice boundary

PACKET-18 is accepted as the backend integrity foundation: immutable 13-item
merge, cited T-01 draft, G-TPL/G-NOTE gating, and one blocked NOTE_REVIEW at
`PACK_READY`. This packet closes the human integrity boundary:

1. Claim-360 readiness and generation controls;
2. the NOTE_REVIEW split workspace with locked evidence and editable commentary;
3. append-version autosave with at most five seconds of browser-loss exposure;
4. human-only signing of an exact graded version;
5. deterministic authority routing and one PACK_REVIEW in the exact role queue;
6. S-3 approve / annotate-and-approve / reject;
7. immutable rejection/revision history and correction capture.

It adds no table or column. `note_drafts` from migration 0014 is sufficient.
Lineage, content hashes, rejection context, and artifact references live in the
structured `body`, while immutable workflow facts live in events.

The production motor pack remains deliberately blocked at two captured-input
gates:

- C-08 is `blocked_on_inputs`, so every current T-01 is `signable: false`.
- T-03 is `pending_capture`, so the > KES 4M side effect cannot render.

Build and test the executable mechanics with synthetic fixture configuration,
but do not make the production motor configuration signable by inventing either
value. The live UI must show the named blockers and the APIs must return a
fail-visible 409 without signing, routing, or moving the FSM.

## 1. Existing contracts to consume unchanged

PACKET-19 consumes these PACKET-18 contracts without reinterpretation:

- readiness response and SHA-256 fingerprint;
- ordered 13-item manifest and source-selection/upload endpoints;
- `pack.merged` event as the immutable merged-version index;
- `note_drafts` exact PRD-08 DDL;
- structured T-01 `body`, including computed, verification, commentary,
  blockers, merged-pack refs, and grader refs;
- NOTE_REVIEW payload with exact pack/draft/artifact refs;
- `RESERVED→PACK_READY` already committed only after the draft integrity gates
  pass.

Do not duplicate merge, commentary generation, or G-NOTE logic in the console.
The server remains authoritative.

## 2. Backend: review workspace and immutable artifact reads

### 2.1 Authenticated workspace read

Add:

```http
GET /reviews/{review_id}/approval-note
```

Only `NOTE_REVIEW{subtype: approval_note}` is accepted. Authorised operational
roles are the existing NOTE_REVIEW roles; cross-claim or non-note ids return
404. The response contains:

```json
{
  "review_id": "…",
  "claim_id": "…",
  "root_draft_id": "…",
  "current_draft": {
    "id": "…",
    "version": 7,
    "status": "in_review",
    "body_sha256": "…",
    "body": {}
  },
  "merged_pack": {
    "event_id": "…",
    "version": 2,
    "sha256": "…",
    "content_url": "/claims/…/approval-pack/artifacts/…"
  },
  "autosave_seconds": 5,
  "signable": false,
  "blockers": []
}
```

`current_draft` is the highest version in the review's lineage, not merely the
id embedded in the original review event. Every read recomputes the canonical
body hash and signability. It never trusts a client-supplied `locked` flag.

### 2.2 Artifact content route

Add:

```http
GET /claims/{claim_id}/approval-pack/artifacts/{event_id}
```

`event_id` must resolve on the same claim to an allowlisted artifact-index event:
`pack.merged` or `pack.note_signed`. Resolve the blob key server-side; never
accept a raw S3/blob key from the browser. Return 404 for a cross-claim ref.
Apply the approval-pack read roles, `application/pdf`, `nosniff`, private/no-store
cache policy, and an ETag equal to the recorded SHA-256.

No public or portal route is added.

## 3. Backend: append-version autosave

Add:

```http
PUT /reviews/{review_id}/approval-note/draft
Idempotency-Key: <non-empty>

{
  "base_draft_id": "…",
  "base_body_sha256": "…",
  "commentary": [
    {"template_slot": "incident_summary", "content": "…"},
    {"template_slot": "excess_commentary", "content": "…"},
    {"template_slot": "savings_narrative", "content": "…"}
  ]
}
```

Binding behaviour:

- claims officer or claims manager only;
- the review must be open and the claim must be `PACK_READY`;
- `base_draft_id` and body hash must equal the latest lineage version;
  otherwise 409 `STALE_NOTE_DRAFT` returns the current id/hash and writes
  nothing;
- accept exactly the three configured commentary slots, once each;
- computed and verification sections, citations, blockers, pack refs, template
  id/version, and integrity refs are copied server-side and cannot be supplied
  or changed by the client;
- retain the ≤80-word incident-summary limit and deterministic numeric
  allow-list checks on every save; invalid commentary is 422 and creates no
  version;
- insert the full new body at `version = max(claim versions)+1`, set it
  `in_review`, set `edited_by`, and mark only the previous unsigned lineage
  version `superseded`;
- store `lineage.root_draft_id`, `lineage.parent_draft_id`,
  `lineage.review_id`, canonical body hash, and the prior generated integrity
  refs inside `body`;
- emit `pack.note_autosaved` with ids, versions, hashes, actor, and review id;
  never put commentary text in the event or ledger;
- same idempotency key plus same request returns the original result; key reuse
  with different content is 409 `IDEMPOTENCY_CONFLICT`;
- signing and autosave take the existing per-claim approval-pack guard, so two
  tabs cannot both become the latest version.

Autosave does not call a model or create/cancel a review item. G-NOTE is rerun
against the exact final candidate at sign time.

## 4. Backend: NOTE_REVIEW@2 and human-only sign

Keep `NOTE_REVIEW@1` for historical replay. Add `NOTE_REVIEW@2` and point the
`approval_note` subtype at it. The schema requires:

```json
{
  "capability_id": "pack.note_draft",
  "draft_id": "…",
  "body_sha256": "…",
  "diff": {
    "typed_changes": [],
    "prose_change_ratio": 0,
    "corrected_prose_ref": "optional immutable autosaved-draft ref"
  },
  "reason": "required on reject"
}
```

The three PRD-04 actions mean:

- **Approve:** sign the current version unchanged.
- **Edit→Approve:** the client must autosave first, then sign that exact saved
  id/hash; it never sends prose through the resolution endpoint.
- **Reject:** do not sign. Return the current version to `draft`, retain it,
  keep the claim `PACK_READY`, and emit `pack.note_review_rejected`. A later
  explicit generation creates a fresh governed candidate.

Approve/Edit→Approve preflight, before `review.resolved`, must:

1. re-read and lock the latest lineage draft;
2. reject stale id/hash with 409;
3. recompute blockers and refuse 409 `SIGN_BLOCKED_ON_INPUTS` if any exist;
4. ensure G-TPL is critical-pass and G-NOTE is pass on the exact final
   candidate bytes; a new failure leaves the review open and creates the
   existing fail-visible EXCEPTION subtype;
5. render the final network-disabled PDF under the pinned PACKET-18 policy;
6. store it immutably and record a `pack.note_sign_prepared` event containing
   only ids, hashes, artifact ref, grader refs, actor, and the route-input
   snapshot.

The durable `review.resolved` consumer finalises idempotently:

- update exactly the prepared draft to `signed`, set `signed_by/signed_at`;
- emit `pack.note_signed` with final-PDF event ref, SHA-256, exact draft and
  merged-pack refs;
- derive routing amount from computed C-08, else the binding `reserve.total`
  fallback, preserving field/calc version provenance;
- call the pinned pack authority matrix;
- create exactly one `PACK_REVIEW{subtype: approval_pack}` carrying
  `routing_amount_cents`, route-input refs, `required_role`,
  `merged_event_id`, `note_signed_event_id`, and hashes;
- emit `pack.routed`;
- transition `PACK_READY→IN_APPROVAL` only after the signed artifact and review
  event are durable.

Replay resumes from the last durable event and produces no second signed
artifact, PACK_REVIEW, or FSM transition. If finalisation has not completed,
Claim 360 shows `signing_pending`; the durable resolution is never reported as
lost.

Human signature is not an autonomy capability. `pack.note_draft` remains max L3
for draft production only; no code path gates or auto-executes the signature.

## 5. Backend: exact authority queue and PACK_REVIEW@2

The existing PACK_REVIEW `band_amount_path: assessment.agreed_quote` is not the
PRD-02 routing contract and must not be used for approval packs. Add the
`approval_pack` subtype with `PACK_REVIEW@2`.

For this subtype:

- band visibility and resolution use the immutable `required_role` and routing
  amount snapshot in the review payload;
- the actor's configured role must equal `required_role`; a higher or different
  role does not silently take the item;
- server-side resolution recomputes the current routing input/version. A changed
  value returns 409 `APPROVAL_ROUTE_STALE`, keeps the item open, and requires a
  fresh route;
- outside-role resolution is 403 `FORBIDDEN_BAND` and appends `authz.denied`;
- `scope=band` includes the item only for its exact required role.

`PACK_REVIEW@2` requires the pack/draft/event refs, route snapshot, and the
existing correction diff. Actions:

- **Approve:** transition `IN_APPROVAL→APPROVED`; signed artifacts remain
  immutable.
- **Edit→Approve:** require a non-empty manager annotation, retain it only in
  the resolution event, mutate no signed artifact, then approve. This is the
  trial's “annotation without Reject”, not rework.
- **Reject:** require structured free-text reasons, transition
  `IN_APPROVAL→PACK_READY`, retain the signed version, clone its structured body
  into a new `in_review` version with a visible `manager_rejection` block, and
  create one new NOTE_REVIEW for the officer. The reasons are review metadata;
  never splice them into generated commentary.

The existing PRD-03 consumer must create one
`origin=production_correction` test case for rejection. If the reasons do not
identify corrected field paths, the case remains visibly
`blocked_on_inputs`; it is still captured and never fabricated.

## 6. > KES 4M and T-03 precedence

PRD-02 precedes PRD-08. Therefore the executable authority contract is:

- amount `> 4_000_000_00` routes approval to `chairman`;
- R-12 renders T-03 for Head of Claims + MD.

Do not implement PRD-08 acceptance text that says the MD is the approval role.
The MD is a T-03 recipient and the ≤KES 4M approval-band owner.

While T-03 is `pending_capture`, sign preflight for a >KES 4M claim returns 409
`ROUTING_BLOCKED_ON_INPUTS` with `blocked_on: open-item-6`; it creates no signed
version, PACK_REVIEW, notification, or FSM hop. Once T-03 becomes live, its
rendered artifact is required before routing can finalise. Internal in-app
notification is live; email may remain honestly staged under open item 1.

Boundary tests are mandatory: exactly `4_000_000_00` → MD, one cent above →
chairman + T-03 side effect.

## 7. ICON note-entry seam

Add a pack-owned `icon.note_entry` field-set configuration slot with:

```yaml
status: pending_capture
blocked_on: open-item-3
fields: []
```

The signed-workspace response exposes that status. Do not invent field order,
selectors, formatting, or a projection operation. The executable paste-assist
strip and readback/reconciliation belong to PRD-09.

## 8. Console implementation

### 8.1 Claim 360 readiness card

On Claim 360:

- load the existing readiness endpoint;
- render all 13 items in manifest order with resolved/ambiguous/missing/
  pending-integration/fallback state and blocker detail;
- explicit items allow selection only from same-claim documents or
  communications already returned by Claim 360;
- items 12–13 accept PDF upload with progress and an accessible error state;
- Generate submits the current readiness fingerprint and idempotency key;
- stale fingerprint 409 refreshes the card and does not claim success;
- generation blocked/staged/completed states use the exact server outcome;
- no blank “unavailable” placeholder remains once the agent is installed.

### 8.2 NOTE_REVIEW workspace

Replace the generic `note_review` payload renderer:

- left: semantic structured note editor;
- computed and verification rows are read-only, display their status, and link
  citations/provenance;
- only the three commentary text areas are editable;
- right: merged PDF through the authenticated artifact endpoint and pdf.js;
- blockers stay visible beside Sign and cannot be dismissed;
- autosave after five seconds of dirty state and on blur; show
  `Saving… / Saved at <EAT> / Save failed`;
- a 409 stale draft reloads the latest server version and preserves the local
  text in a clearly labelled recovery panel; never overwrite silently;
- Approve is labelled **Sign**, Edit→Approve **Save & Sign**, Reject remains
  reason-required;
- keyboard shortcuts remain disabled inside inputs/textareas/contenteditable;
- after reload/crash, the highest server version opens. CI proves at most five
  seconds of unsaved typing.

### 8.3 S-3 Approval workspace

Replace `approval-pack-unavailable`:

- band queue lists only `PACK_REVIEW{subtype: approval_pack}` for the actor's
  exact role;
- merged PDF and signed final note render side by side;
- route amount is rendered from integer cents with its provenance;
- Approve, Annotate & Approve, Reject with reasons map to the three closed
  actions;
- T-03 state is visible for >KES 4M;
- outside-band, stale-route, or blocked-side-effect errors are explicit and do
  not optimistically remove the item;
- after manager rejection, navigate to the new NOTE_REVIEW/Claim 360 state.

Support 1366×768 and keyboard-only operation. Add no global key handlers.

## 9. Events, ledger, security, and idempotency

Register and map through the existing single ledger writer:

- `pack.note_autosaved`
- `pack.note_review_rejected`
- `pack.note_sign_prepared`
- `pack.note_signed`
- `pack.routed`

`review.resolved`, `authz.denied`, `template.rendered`, and
`claim.status_changed` remain their existing canonical events. Do not add
duplicate approval/rejection event types.

Events and logs contain ids, hashes, state, role, cents, and provenance refs;
never commentary text, manager free text, or decrypted PII. Artifact access is
RBAC-checked and access-logged. No claim fields are updated in this packet.

All write endpoints require an idempotency or optimistic-concurrency token.
Retries after an uncertain immutable-store result must verify the expected
content hash before continuing; they never blind-write a second artifact.

## 10. Acceptance

Protected backend tests pin:

1. motor-v1 live note refuses sign with the named C-08 blocker and no mutation;
2. autosave inserts versions, retains history, rejects stale tabs, and replays
   one idempotency key exactly once;
3. locked-section tampering and an injected number are rejected;
4. final sign grades and signs the exact saved hash; crash/replay produces one
   signed event, one PACK_REVIEW, and one FSM hop;
5. exactly KES 4M routes MD; KES 4M + 1 cent blocks visibly on T-03 in the
   production pack and routes chairman in a fixture where T-03 is live;
6. exact-role queue visibility; wrong/out-of-band actor gets 403 plus
   `authz.denied`;
7. manager approve reaches APPROVED; annotation mutates no artifact; rejection
   returns PACK_READY, retains the signed version, opens one new NOTE_REVIEW,
   and captures one correction case;
8. cross-claim artifact fetch is 404 and raw blob keys are never accepted;
9. ICON note-entry remains `pending_capture` with no field order.

Protected console tests pin:

1. 13-row readiness card, source selection, PDF upload, stale refresh, and
   generate states;
2. split NOTE_REVIEW workspace, locked evidence, editable commentary, citation
   interaction, and pdf.js merged pack;
3. fake-timer autosave at five seconds, reload recovery, stale-tab recovery,
   save failure, and no shortcut firing while typing;
4. S-3 exact-band queue and side-by-side artifacts;
5. approve, annotation, rejection, and server-error states;
6. axe pass and usable 1366×768 layout.

Live/manual gates remain explicit:

- generation <2 minutes;
- officer review + sign ≤8 minutes;
- side-by-side manager sign-off with the current Mayfair PDF;
- Chromium/S3 Object-Lock certification;
- C-08 and T-03 capture.

Synthetic timing or screenshot assertions do not discharge these gates.

## 11. Definition of done

- `ruff check .`, money lint, banned-call lint, full SQLite and PostgreSQL suites
  green;
- backend pooled branch coverage ≥80%, pack calcs 100%, frontend ≥70%;
- no migration and no DDL drift;
- OpenAPI snapshot includes the workspace, autosave, and artifact routes plus
  existing approval-pack routes;
- approval-pack and status-console runbooks cover stale edits, save failure,
  signing recovery, route staleness, T-03/C-08 blockers, rejection revision,
  artifact-access denial, and idempotent replay;
- grader map covers final signed T-01 with G-TPL critical and G-NOTE;
- PR description lists register #246–#254, protected test additions, OpenAPI
  diff, coverage, and all remaining live blockers;
- no PRD-09 adapter, direct external write, payment operation, portal route, or
  new review type.

## 12. Builder hand-off

Implement in this order:

1. subtype schemas/contracts and exact-role server guard;
2. workspace/artifact reads;
3. autosave lineage and concurrency;
4. sign preparation + idempotent finaliser;
5. PACK_REVIEW approve/reject loop;
6. Claim-360 card;
7. NOTE_REVIEW editor;
8. S-3 workspace;
9. OpenAPI, runbooks, coverage, full regression.

Stop and append a new ED-11 register entry before making any choice not fixed
above. Do not solve a live discovery dependency in code.
