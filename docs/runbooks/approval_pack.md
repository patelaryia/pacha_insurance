# Approval-pack backend runbook

## What is live

PACKET-18 owns the PRD-08 back half up to `PACK_READY`: manifest source selection, the
readiness card, the immutable merged PDF, the structured T-01 draft, both integrity graders,
and one open `NOTE_REVIEW{approval_note}`. Editing, autosave, signing, authority routing,
T-03 and ICON paste-assist belong to PACKET-19. Resolving this packet's `NOTE_REVIEW`
returns `409 NOTE_REVIEW_UI_NOT_BUILT`, changes no row, and emits no FSM transition — that
is the expected launch behaviour, not a defect.

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
and after every attempt. A breach creates `EXCEPTION{budget_exceeded}`, drafts no note, and
leaves the claim `RESERVED`. Raise the pack budget rather than retrying the request.

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
(register #239), and never touches a signed row or any stored bytes. With a fixed clock and
unchanged sources the rebuilt bytes are identical, because converted sources are
content-addressed and reused (register #227).

## Immutable-store outage

Merge writes only through the `ImmutableArtifactStore` protocol. The local adapter is
write-once: an overwrite with different bytes is refused rather than applied, and the event
records `object_lock_status=local_write_once`. Real S3 Object Lock certification remains
open (#30/#116/#226), so production publishability is still blocked. Orphaned bytes from a
crash are never promoted into the `pack.merged` event index and are never guessed back into
use — regenerate instead.

## PACKET-19 hand-off

`GET /claims/{id}/approval-pack/versions` and `GET /claims/{id}/approval-pack/note-drafts`
are the read surface PACKET-19 consumes without reinterpretation. PACKET-19 owns the editor,
autosave, signature, `PACK_READY→IN_APPROVAL`, authority routing and T-03, and must resolve
the >4M chairman/MD contradiction (#235) before asserting that scenario. No draft carrying a
blocked slot or a failed grader may ever be signed.

## Generated OpenAPI

`python tools/openapi_snapshot.py` rewrites `docs/openapi/approval_pack.json`; the
`--check` form fails when the committed artifact is stale and runs in the unit suite. The
surface is exactly six routes: readiness, source selection, item upload, generation, and the
two read-only PACKET-19 feeds. There is no sign, route, or approve endpoint.

## Dependencies

`pypdf` performs concatenation and outlines. The production HTML renderer must launch only
the pinned Chromium executable and version supplied by runtime config and refuses startup on
a mismatch; live Chromium and S3 Object Lock certification remain explicit production
blockers, not a completed item.
