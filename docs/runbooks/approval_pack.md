# Approval-pack backend runbook

## What is live

PACKET-18 owns the PRD-08 back half up to `PACK_READY`: manifest source selection, the
readiness card, the immutable merged PDF, the structured T-01 draft, both integrity graders,
and one open `NOTE_REVIEW{approval_note}`. PACKET-19 adds the review workspace, five-second
append-version autosave, human-only signing, deterministic authority routing, the S-3
approval loop, and the authenticated artifact reads. ICON paste-assist remains PRD-09.

Two production blockers are deliberately preserved and are the expected launch behaviour,
not defects:

- **C-08 is `blocked_on_inputs`**, so every current T-01 has `signable: false`. Signing a
  live motor note returns `409 SIGN_BLOCKED_ON_INPUTS` naming the payable blocker and
  mutates nothing.
- **T-03 is `pending_capture`**, so a >KES 4M claim cannot render its mandated alert. Sign
  preflight returns `409 ROUTING_BLOCKED_ON_INPUTS{blocked_on: open-item-6}` and creates no
  signed version, `PACK_REVIEW`, notification, or FSM hop.

Neither value is invented anywhere in code, configuration, or fixtures.

## Who may do what

Only a claims officer or a claims manager may select sources, upload an item, or
request generation; every other role receives `403 FORBIDDEN_ROLE`. The readiness card
carries claim source identifiers, so it is readable by the operational and approval roles
but never by an auditor and never from a portal route.

## Resolving manifest ambiguity

`GET /claims/{id}/approval-pack/readiness` always returns exactly 13 ordered items. An item
in state `ambiguous` lists every candidate id; the engine never picks. Resolve it with

```bash
curl -X PUT "$BASE/claims/$CLAIM/approval-pack/manifest/claim_form/sources" -H "X-Actor: $USER" -H 'Content-Type: application/json' -d '{"sources":[{"kind":"document","id":"<document id>"}]}'
```

Selection is append-only: the latest valid `pack.sources_selected` event wins and no prior
event is updated or deleted. Repeating the same ordered selection is a 200 that emits
nothing. A source belonging to another claim returns 404, deliberately indistinguishable
from absent. Items 1, 2, 9 and 11 always require explicit selection (register #223); item 10
resolves only from `assessment.selection_completed`, or from the sole unambiguous
`assessment.report_received` document on a single-assessor claim. Multiple revisions from one
firm remain a hard block (#216).

Items 12 and 13 report `pending_integration` until PRD-09 emits a captured artifact
(register #224). Upload the officer PDF instead:

```bash
curl -X POST "$BASE/claims/$CLAIM/approval-pack/manifest/claim_details_report/upload" -H "X-Actor: $USER" -F file=@claim-details.pdf
```

No manifest item is waivable at launch (#222). Do not infer a waiver from a similarly named
chase item.

## Stale fingerprint

Generation requires the fingerprint from the card the officer actually read. If any material
input changed in between, the request returns `409 READINESS_STALE` (or `PACK_NOT_READY`)
with the recomputed card, emits exactly one `pack.generation_refused` for that
`Idempotency-Key`, and creates no partial artifact. Re-read the readiness card and resend.
The same key always replays the original outcome; a new key is an explicit regeneration that
creates the next immutable pack version and retains every earlier version and its bytes.

## Inspecting a fallback

Each `pack.merged.manifest[].sources[]` entry carries `fallback_used`, `fallback_reason` and
`blocked_resource_count`. Only a renderer timeout or crash triggers the single deterministic
plaintext fallback; Chromium is never retried. `blocked_resource_count` above zero is normal
— the policy is offline by construction and remote fonts and images are never fetched.
Invalid or absent archived communication bytes are a readiness blocker, never a blank page.

## Failed commentary

Unverifiable prose never reaches a signer. The post-processor requires the exact three
paragraphs, numeric multiset equality between `numbers_used` and the rendered text, and
membership in the allowed-number multiset derived from verified fields and cited savings
rows. The first failure triggers exactly one governed regeneration carrying only validation
errors. The second creates `EXCEPTION{note_commentary_invalid}` with the four-part contract,
creates no note draft, and leaves the claim `RESERVED`. Fix the cited claim input — never
edit the model's prose by hand to make it pass.

## Failed merged-pack grade

The merged pack is graded by critical G-TPL before its `pack.merged` event is appended. A
result other than `pass` stops the request: no version is indexed, no note is drafted, the
claim stays `RESERVED`, and one `EXCEPTION{pack_integrity_failed}` opens. The request
returns `409 PACK_GENERATION_BLOCKED` naming the exception subtypes (register #240). The
bytes written before the grade are orphaned by design and are never promoted.

## Model budget

Commentary generation and its single regeneration share one per-call ceiling, and the
claim-day, claim-lifetime and platform-day budgets in `commentary.yaml` are checked before
and after every attempt under a platform-wide budget lock. The configured maximum call cost
is checked after provider billing, exact provider token usage is preferred, and a conservative
UTF-8 upper bound is used when an injected provider supplies no usage. A breach creates
`EXCEPTION{budget_exceeded}`, drafts no note, and leaves the claim `RESERVED`. Raise the pack
budget rather than retrying the request.

## Failed grader

If G-TPL or G-NOTE returns anything other than `pass`, the candidate is persisted as
`note_drafts.status='draft'` with its integrity result and one
`EXCEPTION{note_integrity_failed}` is created. No `NOTE_REVIEW` opens and the claim stays
`RESERVED` (register #233). Read the recorded grader run before regenerating: a
`NUMERIC_SOURCE_MISMATCH` means the note cited a value that does not match its current claim
field, calc run, or savings row, and `NUMERIC_TOKEN_OMISSION` means the grader model failed
to account for every number in the commentary.

## Regenerating safely

Use a new `Idempotency-Key` with a fresh readiness fingerprint. Reusing a key is a true
replay: the request event, the staged action and the indexed version are all allocated once
under the claim lock, so a duplicate submission can never create a second pack. Regeneration
creates pack version N+1 and note version N+1, marks only an unsigned `draft`/`in_review`
predecessor `superseded`, cancels that predecessor's NOTE_REVIEW so exactly one stays open
(register #239), and never touches a signed row or any stored bytes. Cancellation is allowed
only for `NOTE_REVIEW/approval_note` by the actor on its originating `review.created` event;
the `review.cancelled` event carries the stable source-event id so projection rebuilds retain
the cancellation. With a fixed clock and
unchanged sources the rebuilt bytes are identical, because converted sources are
content-addressed and reused (register #227).

## Immutable-store outage

Merge writes only through the `ImmutableArtifactStore` protocol. The local adapter is
write-once: an overwrite with different bytes is refused rather than applied, and the event
records `object_lock_status=local_write_once`. Real S3 Object Lock certification remains
open (#30/#116/#226), so production publishability is still blocked. Orphaned bytes from a
crash are never promoted into the `pack.merged` event index and are never guessed back into
use — regenerate instead.

## Review workspace, autosave, and stale edits

`GET /reviews/{review_id}/approval-note` returns the *highest* version in the review's
lineage, never the id embedded in the original review event. Every read recomputes the
canonical body hash and the blocker list, so a client-supplied `locked` or `signable` flag
is never trusted.

`PUT /reviews/{review_id}/approval-note/draft` appends a new version. It accepts only the
three configured commentary slots; computed and verification sections, citations, blockers,
pack refs, template id/version and integrity refs are copied server-side. Every save reruns
the deterministic numeric allow-list and the ≤80-word incident-summary limit, so an injected
figure is a 422 that creates no version.

- **Stale tab** — a `base_draft_id`/`base_body_sha256` that is not the latest lineage version
  returns `409 STALE_NOTE_DRAFT` with the current id, version and hash, and writes nothing.
  The console reloads the server version and preserves the local text in a labelled recovery
  panel; it never overwrites silently.
- **Save failure** — the console shows `Save failed` with the exact server code. Retrying is
  safe: reusing the same idempotency key with the same content replays the original result,
  and reusing it with different content is `409 IDEMPOTENCY_CONFLICT`.
- **Lost work** — the pack-configured `autosave_seconds` (5) bounds the exposure. After a
  crash or reload the workspace opens the highest server version.

Commentary text never enters an event or the audit ledger; `pack.note_autosaved` carries ids,
versions, hashes and the actor only.

## Signing recovery

Signing is human-only. `pack.note_draft` remains max L3 for draft production; no code path
gates or auto-executes a signature. Preflight, before `review.resolved` is recorded:

1. re-read and lock the latest lineage draft; a stale id/hash is `409 STALE_NOTE_DRAFT`;
2. recompute blockers; any blocker is `409 SIGN_BLOCKED_ON_INPUTS` — on the production motor
   pack this always fires with the named C-08 blocker;
3. resolve the routing input and refuse an uncaptured mandated side effect (below);
4. re-grade G-TPL (critical) and G-NOTE on the exact final candidate bytes; a new failure
   leaves the review open and creates `EXCEPTION{note_integrity_failed}`;
5. render the final network-disabled PDF under the pinned PACKET-18 policy and store it
   immutably;
6. record `pack.note_sign_prepared`.

The durable `review.resolved` event, not the HTTP request, triggers finalisation. If the
process dies between preparation and finalisation, Claim 360 and the workspace report
`sign_state: signing_pending` — the resolution is never reported as lost. Replay resumes from
the last durable event and produces no second signed artifact, `PACK_REVIEW`, or FSM hop:
`pack.note_signed`, `pack.routed` and the `PACK_READY→IN_APPROVAL` transition are each guarded
by their own evidence. Re-preparation is content-addressed, so an identical candidate reuses
the same immutable key; a differing overwrite is refused as `409 UNCERTAIN_WRITE` with
`EXCEPTION{uncertain_write}` rather than blind-writing a second artifact.

**NOTE_REVIEW Reject** signs nothing. The current version is retained and returned to
`draft`, the claim stays `PACK_READY`, and `pack.note_review_rejected` is emitted. A fresh
governed candidate requires an explicit new generation; nothing regenerates silently.

## Authority routing, route staleness, and the T-03 blocker

PRD-02 §2.5 precedes PRD-08. The routed amount is C-08 payable when that calculation is live
and the binding `reserve.total` fallback while it is not; the snapshot records which, with the
calc-run or field id and version. Bands are inclusive: exactly `4_000_000_00` routes to the
**MD**; one cent above routes to the **chairman** and carries the `render T-03` side effect.
The MD is a T-03 recipient and the ≤KES 4M band owner — never the >4M approval role.

While T-03 is `pending_capture`, a >KES 4M sign preflight returns
`409 ROUTING_BLOCKED_ON_INPUTS{blocked_on: open-item-6}` and creates no signed version,
`PACK_REVIEW`, notification, or FSM hop. Once T-03 is live its artifact is rendered before the
approval item exists. In-app notification to Head of Claims and the MD is live through
`notify`; the email body remains honestly staged under open item 1/6.

`PACK_REVIEW{approval_pack}` is authorised against the immutable `required_role` in its
payload, not `assessment.agreed_quote`. A wider band does not silently take another role's
item: `scope=band` shows it only to the exact role, and a different role is `403
FORBIDDEN_BAND` with an `authz.denied` event. Server-side resolution recomputes the routing
input; a changed value is `409 APPROVAL_ROUTE_STALE`, the item stays open, and a fresh route
is required.

## Manager approval, annotation, and rejection revision

- **Approve** transitions `IN_APPROVAL→APPROVED`. Signed artifacts stay immutable.
- **Annotate & Approve** requires a non-empty manager annotation, retains it only in the
  resolution event, mutates no signed artifact, then approves.
- **Reject** requires structured `{code, detail}` reasons (an optional `field_path` must also
  appear in the typed diff), transitions `IN_APPROVAL→PACK_READY`, retains the signed version,
  clones its structured body into a new `in_review` version carrying a visible
  `manager_rejection` block, and opens one new NOTE_REVIEW. Reasons are review metadata and
  are never spliced into generated commentary. The PRD-03 consumer captures one
  `origin=production_correction` case; when the reasons name no corrected field path the case
  is captured but stays visibly `blocked_on_inputs`.

## Artifact access denial

`GET /claims/{claim_id}/approval-pack/artifacts/{event_id}` accepts only an allowlisted
`pack.merged` or `pack.note_signed` event id on the same claim. Raw S3/blob keys are never
accepted from a browser. A cross-claim event id, a non-allowlisted event, or an unknown id is
`404 ARTIFACT_NOT_FOUND`; an actor outside the approval-pack read roles is `403`. Responses
carry `application/pdf`, `nosniff`, `private, no-store`, and an ETag equal to the recorded
SHA-256. No public or portal route exists.

## ICON note entry

`packs/motor/approval_pack/icon.yaml` ships `icon.note_entry` as `pending_capture`,
`blocked_on: open-item-3`, with an empty field list. The workspace response exposes that
status verbatim. Field order, selectors, formatting, and the executable paste-assist strip
belong to PRD-09 — none of them may be invented here.

## Generated OpenAPI

`python tools/openapi_snapshot.py` rewrites `docs/openapi/approval_pack.json`; the
`--check` form fails when the committed artifact is stale and runs in the unit suite. The
surface is nine routes: readiness, source selection, item upload, generation, the two
read-only version feeds, the authenticated artifact read, and the review-scoped workspace
read and autosave. There is no sign, route, or approve endpoint — signing and approval both
travel through the closed PRD-04 `/reviews/{id}/resolve` contract.

## Dependencies

`pypdf` performs concatenation and outlines. The production HTML renderer must launch only
the pinned Chromium executable and version supplied by runtime config and refuses startup on
a mismatch; live Chromium and S3 Object Lock certification remain explicit production
blockers, not a completed item.
