# PACKET-18 — Approval-pack backend: readiness, immutable merge, cited T-01 draft, integrity graders (PRD-08 slice 1 of 2)

> **Status:** issued · **Builder:** Codex per `AGENTS.md` · **Reviewer:** CTO per
> `CLAUDE.md`
> **Source spec:** `docs/PRD-08_Approval_Pack_Generator_v1.1.md` §8.1–§8.4,
> §8.5 persistence only, §8.6 (`pack.merge`, `pack.note_draft`), §8.7 (1)–(3);
> PRD-00 §0.4 (`RESERVED→PACK_READY`); PRD-01 §1.4; PRD-02 §2.3–§2.5
> (StrictUndefined, T-01, C-08, authority data); PRD-03 §3.3 (G-NOTE/G-TPL,
> autonomy); PRD-04 §4.3 (`NOTE_REVIEW`, closed enum); PRD-07 §7.6
> (`savings_ledger`); PRD-13 §13.2/§13.4; Section 0.5 AR-1–AR-5; Section 0
> ED-1/ED-6a/ED-8/ED-9/ED-11; guide §3/§4/§6; registers
> #5/#6/#30/#49/#61/#62/#67/#75/#81/#116/#130/#157/#200/#208/#218.
> Precedence: Section 0 → Section 0.5 → PRD-00/01/02/03/04/07/08/13 →
> PACKET-01..17 contracts → this packet.
> **Depends on:** PACKET-17 merged and green, including owner correction #218.
> This packet is cut as a stacked branch from `codex/packet-17`; it is not
> mergeable before that dependency.
> **Acceptance:** `tests/acceptance/test_packet_18_approval_pack_backend.py` —
> protected, failing by design until this packet is built.
> **Packet 19 (final PRD-08 slice):** Claim-360 readiness UI; note editor and
> citation interactions; 5-second append-version autosave and crash recovery;
> human sign/final PDF; `PACK_READY→IN_APPROVAL`; authority routing, manager
> approval/rejection and production-correction capture; >4M T-03; ICON
> paste-assist; timed and visual acceptance.

## 0. Slice boundary

The former proposed Packets 18 and 19 are one coherent integrity boundary here:
the manifest/readiness/merge backend and the structured T-01 generator ship
together. A merge without its cited note consumer would leave an ungraded
artifact seam; a note generator without the exact immutable pack version it
cites would be equally unsafe.

This packet starts with a claim in `RESERVED` and ends with:

1. an immutable, versioned merged PDF whose 13 manifest items, source ids,
   page ranges, fallback flags, digest and render timestamp are durable;
2. a structured `note_drafts` version linked to that exact pack version;
3. a deterministic T-01 review artifact with locked computed/verification
   sections and editable commentary sections;
4. synchronous G-TPL and G-NOTE results attached to the candidate;
5. one open `NOTE_REVIEW` item and `RESERVED→PACK_READY` only when both artifact
   integrity gates pass.

It deliberately does **not** implement a usable NOTE_REVIEW resolution, editing,
autosave, signing, approval routing, T-03, or ICON field set. Until Packet 19,
attempting to resolve this packet's `NOTE_REVIEW{subtype: approval_note}` returns
`409 NOTE_REVIEW_UI_NOT_BUILT`, changes no row, and emits no FSM transition.
There is therefore no accidental back door to `IN_APPROVAL`.

## 1. Scope

### 1.1 Package, installation, and data model

Build `agents/approval_pack_agent/` (import `approval_pack_agent`; proposed
#219) with the sole installer:

```python
build_approval_pack_agent(
    app,
    *,
    model_client=None,
    html_renderer=None,
    immutable_store=None,
    config=None,
) -> ApprovalPackAgent
```

Install after `build_assessment_agent`, `build_agent_runtime`,
`build_eval_harness`, and `build_review_queue`. The installer is idempotent,
registers its routes/consumers/action executors once, exposes the curated public
service as `app.state.approval_pack_agent`, and imports no private module from
another PRD package (ED-1).

Migration `0014_note_drafts` creates the PRD-08 §8.5 table **exactly**:

```sql
CREATE TABLE note_drafts (
  id TEXT PRIMARY KEY,
  claim_id TEXT NOT NULL,
  version INT NOT NULL,
  body JSONB NOT NULL,
  status TEXT NOT NULL,
  edited_by TEXT,
  signed_by TEXT,
  signed_at TIMESTAMPTZ,
  UNIQUE (claim_id, version)
);
```

Binding details:

- `body` uses the repository's JSONB/SQLite JSON compatibility type.
- `status` has a DDL check for exactly
  `draft|in_review|signed|superseded`.
- `claim_id` is a foreign key to `claims.id`; no cascade delete (ED-9).
- `version >= 1`; `signed_at` is nullable timezone-aware.
- Do **not** add timestamps, pack ids, artifact keys, grader columns, edit
  metadata, or soft-delete flags. Exact references live inside `body`; adding a
  convenience column would violate the binding DDL.
- The model lives on `claim_core.Base`; Alembic imports it via the package's
  public model-registration hook. PostgreSQL and SQLite upgrades/downgrades are
  both tested.

Merged-pack metadata needs no locally invented table. `pack.merged` events are
the append-only version index; each event carries the complete contract in §3.3.
Version assignment locks the claim row, reads the highest earlier
`pack.merged.payload.version`, and appends the next integer. Concurrent requests
must yield distinct monotonic versions or one clean idempotent replay — never two
events with the same `(claim_id, version)` (proposed #220).

### 1.2 Pack-owned manifest and source selection

Add `packs/motor/approval_pack/manifest.yaml`, validated at startup. The schema
is deliberately small (proposed #221):

```yaml
version: 1
items:
  - id: policy_document
    order: 1
    label: Policy document/schedule
    source_kinds: [document]
    selector: explicit
    repeatable: false
    conversion: passthrough
    required: true
    waivable: false
```

Every item has exactly `{id, order, label, source_kinds, selector, repeatable,
conversion, required, waivable}`. A `doc_types` key exists only for an
`auto_doc_type` selector. Startup rejects unknown keys, duplicate ids/orders,
non-contiguous order 1–13, unknown source/conversion values, or a referenced
document type absent from PRD-01's registry. The exact motor-v1 rows are:

|order|id|source kinds|selector|repeatable|conversion|
|---:|---|---|---|:---:|---|
|1|`policy_document`|document|explicit|no|passthrough|
|2|`intimation_email`|communication|explicit|no|html_to_pdf|
|3|`claim_form`|document|auto_doc_type `claim_form`|no|passthrough|
|4|`logbook`|document|auto_doc_type `logbook`|no|passthrough|
|5|`driving_licence`|document|auto_doc_type `driving_licence`|no|passthrough|
|6|`kra_pin_cert`|document|auto_doc_type `kra_pin_cert`|no|passthrough|
|7|`photos`|document|auto_doc_type `photo_damage`|yes|photos_2up|
|8|`repair_estimate`|document|auto_doc_type `repair_estimate`|no|passthrough|
|9|`assessor_engagement_email`|communication|explicit|no|html_to_pdf|
|10|`assessor_report`|document|selected_assessor_report|no|passthrough|
|11|`supplier_quotes`|communication, document|explicit|yes|source_default|
|12|`assessor_payment_request`|projection_readback, upload|projection_or_upload|no|passthrough|
|13|`claim_details_report`|projection_readback, upload|projection_or_upload|no|passthrough|

All 13 rows are `required: true`, `waivable: false` in this first executable
manifest (proposed #222). PRD-08 says missing **non-waived** items refuse but
only explicitly defines 12–13 as non-waivable; no item→waiver authority or
evidence contract exists. Treating every item as non-waivable is the narrowest
safe launch behaviour. Do not infer a waiver from a chase item with a similar
name. A later pack version may enable a captured waiver contract.

Items 1 and 11 intentionally use explicit source selection rather than invented
`policy_schedule`/`supplier_quote` document types (proposed #223). Items 2 and 9
also require explicit selection: communications have no semantic-purpose column,
and neither “first email” nor “nearest timestamp” is safe. Item 10 resolves only
from the authoritative document in `assessment.selection_completed`; for a
single-assessor claim it may use the sole unambiguous
`assessment.report_received` document. Revision ambiguity #216 remains a hard
block.

Expose the append-only source selection API:

```http
PUT /claims/{claim_id}/approval-pack/manifest/{item_id}/sources
X-Actor: user:<ulid>
Content-Type: application/json

{"sources":[{"kind":"document|communication","id":"..."}, ...]}
```

- Claims-officer or claims-manager roles only; 403 otherwise.
- Validate claim ownership, allowed source kind, required cardinality, and
  source conversion compatibility before recording anything.
- A non-repeatable item requires exactly one source; a repeatable item requires
  one or more distinct sources.
- The operation emits `pack.sources_selected` with item id, complete ordered
  source refs, actor, and prior event id. It never updates/deletes the prior
  event. Latest valid event wins.
- Repeating the same ordered selection is an idempotent 200 and emits nothing.
- A source belonging to another claim is 404 (do not leak its existence).
- Automated selectors never choose among >1 candidates. They report
  `ambiguous_sources` with candidate ids on the authenticated readiness API.

Items 12–13 additionally expose:

```http
POST /claims/{claim_id}/approval-pack/manifest/{item_id}/upload
X-Actor: user:<ulid>
Content-Type: multipart/form-data; file=<PDF>
```

Only those two ids accept this route. Validate PDF magic and parseability,
store immutably, and emit `pack.item_uploaded` carrying `{item_id, upload_id,
blob_key, filename, mime, sha256, received_at}`. A later upload supersedes only
for resolution; old bytes/events remain. Projection readback is a declared
resolver seam returning `pending_integration` until PRD-09 emits a captured
artifact (proposed #224); upload is fully live, so the manifest is satisfiable
now. Do not invent a projection event name or scrape ICON state.

### 1.3 Readiness engine and authenticated API

Expose:

```http
GET /claims/{claim_id}/approval-pack/readiness
```

Response contract (§3.1) is the backend for Packet 19's Claim-360 card. It is a
pure read: no source selection, field write, conversion, model call, or event.

Readiness is true only when all are true:

1. claim FSM is exactly `RESERVED` for initial generation, or `PACK_READY` for
   an explicit regeneration request whose latest note is not `signed`;
2. every claim-doc and assessor-report chase checklist that exists is complete,
   and at least one `purpose='claim_docs'` checklist exists; `surrender`
   checklists are irrelevant here;
3. each of the 13 manifest rows resolves to the required cardinality without an
   ambiguity, missing blob, digest mismatch, rejected document, or invalid PDF;
4. every active T-01 required field in §1.7 is present at
   `human_verified` (the registry's binding floor); and
5. no open `EXCEPTION` subtype that names pack source ambiguity, assessment
   report revision ambiguity, or note integrity failure exists.

The readiness response is recomputed from current durable inputs and includes a
SHA-256 `fingerprint` over canonical JSON of claim status, checklist terminal
states, manifest version and resolved source ids/digests, current required-field
ids/versions, selected consistency-result ids, savings-ledger row ids/amounts,
and latest pack/note versions. The generation route requires that fingerprint;
if anything changes between read and execute it returns
`409 READINESS_STALE` with the new card, preventing a time-of-check/time-of-use
pack (proposed #225).

PII/source ids are returned only to authenticated operational/approval roles.
No public/portal route is added.

### 1.4 Conversion policy

Create explicit protocols so acceptance never needs a live browser or S3:

```python
class HtmlPdfRenderer(Protocol):
    def render(self, html: str, *, policy: HtmlRenderPolicy) -> HtmlRenderResult: ...

class ImmutableArtifactStore(Protocol):
    def put_immutable(self, key: str, content: bytes, *, retention: str) -> None: ...
```

`HtmlRenderPolicy` is pack-loaded and contains: `network_enabled=false`, allowed
schemes exactly `[data, cid]`, `page=A4`, `orientation=portrait`, margins 18mm,
fixed viewport, print CSS enabled, timeout 30 seconds, UTC internal render time,
and EAT timestamp-header format. The production adapter launches only the pinned
Chromium executable/version supplied by runtime config; version mismatch refuses
startup. Request interception rejects HTTP(S), file, websocket and unknown
schemes. It never fetches a remote font/image even if the source HTML requests
one. CID parts already archived with the communication may be rewritten to
`data:`; unresolved CIDs become an explicit blocked-resource marker.

On renderer timeout/crash only, perform one deterministic plaintext fallback
using archived subject/from/to/date/body text, A4 and the same timestamp header;
set `fallback_used=true`, `fallback_reason=html_renderer_timeout|crash`, and
include blocked resource counts in the manifest. Do not retry Chromium. Invalid
or absent archived communication bytes is a readiness blocker, not a blank page.

Photos are converted with Pillow/PyMuPDF into two images per A4 page in resolved
source order. Each caption is exactly `{filename} — received {YYYY-MM-DD EAT}`.
No EXIF time, resizing randomness, or remote content. One final unpaired photo
occupies the first slot with the second left blank.

Passthrough accepts parseable PDFs only. A non-PDF source for a passthrough row
is `conversion_unsupported`; do not silently image or OCR it. Item 11 applies
HTML conversion to communications and passthrough to PDFs (`source_default`).

The local immutable-store adapter uses unique version/digest keys and refuses an
overwrite with different bytes. It records `object_lock_status=local_write_once`.
Real S3 Object Lock remains infrastructure register #30/#116; production
publishability must remain visibly blocked until that adapter proves compliance
(proposed #226). The application contract must already be Object-Lock-shaped;
do not call ordinary mutable `.put()` from merge code.

### 1.5 Merge engine (`pack.merge`)

Add `pypdf` as the merge dependency and use it for concatenation and outlines.
The engine:

1. rechecks the supplied readiness fingerprint inside the claim lock;
2. renders a cover page whose visible table columns are exactly
   `Item | Source document | Received date | Pages`;
3. converts each resolved source under §1.4;
4. appends sources in manifest order; repeatable sources sort by
   `(received_at, kind, id)` unless an explicit selection event supplied an
   order, in which case that captured order is authoritative;
5. creates exactly 13 top-level bookmarks, labels equal to manifest `label`,
   each pointing to that item's first page (cover has no manifest bookmark);
6. sets deterministic PDF metadata and removes random producer ids;
7. names the output exactly `All Docs merged for {vehicle.reg}.pdf`;
8. calculates SHA-256 on final bytes, stores under
   `approval-packs/{claim_id}/merged/v{version}/{sha256}.pdf` with retention
   `claim_record`; and
9. appends `pack.merged` and lets the existing concurrency-1 ledger writer
   record it.

Every source page is copied; no rasterisation or content rewrite occurs for
passthrough PDFs. Cover page `Pages` is the final inclusive pack page range for
that source. The manifest JSON records both source-page count and final range.

With an injected fixed clock and identical source bytes, selection, manifest and
renderer, two merge builds must be byte-identical. With only render time changed,
normalising/removing the visible EAT timestamp header must leave byte-identical
page content and structure. No other metadata may drift (proposed #227).

Generation endpoint:

```http
POST /claims/{claim_id}/approval-pack/generate
X-Actor: user:<ulid>
Idempotency-Key: <non-empty opaque string>
Content-Type: application/json

{"readiness_fingerprint":"<sha256>"}
```

It is governed through `execute_or_stage(capability_id='pack.merge')`. At launch
L1, return 202 with the DRAFT_RELEASE id and create **no** PDF/upload/note. Human
release executes the captured fingerprint/request. At L3 in tests, execution is
immediate. Same claim + idempotency key returns the original outcome; a new key
is an explicit regeneration and creates a new immutable pack version. After a
merge succeeds it invokes `pack.note_draft` through `execute_or_stage` — never a
direct call around the second capability gate.

Any readiness failure returns 409 `PACK_NOT_READY` with the complete blocker
list, emits one idempotent `pack.generation_refused` event for the request key,
and creates no partial merged artifact/event. A conversion failure after the
readiness read is fail-closed and visible as
`EXCEPTION{subtype: pack_conversion_failed}` with the four-part contract; no
`pack.merged` event is written. Orphaned immutable bytes from a crash are not
promoted into the event index and are never guessed back into use.

### 1.6 Merged-pack event contract

`pack.merged.payload` is exactly:

```json
{
  "version": 1,
  "filename": "All Docs merged for KBX 123A.pdf",
  "blob_key": "approval-packs/<claim>/merged/v1/<sha>.pdf",
  "sha256": "<64 lowercase hex>",
  "rendered_at": "<UTC RFC3339>",
  "object_lock_status": "local_write_once|s3_object_lock",
  "readiness_fingerprint": "<sha>",
  "manifest_version": 1,
  "manifest": [
    {
      "item_id": "policy_document",
      "label": "Policy document/schedule",
      "bookmark": "Policy document/schedule",
      "sources": [
        {
          "kind": "document",
          "id": "<id>",
          "filename": "policy.pdf",
          "received_at": "<UTC RFC3339>",
          "sha256": "<sha>",
          "source_pages": 2,
          "pack_pages": [2, 3],
          "fallback_used": false,
          "fallback_reason": null,
          "blocked_resource_count": 0
        }
      ]
    }
  ]
}
```

Manifest entries and sources are ordered. JSON keys are canonical on hashing.
No raw email body, field value, party name, or model prose goes into this event;
the ledger receives references/digests, not a second PII copy.

Add event-catalog and audit action mappings for `pack.sources_selected`,
`pack.item_uploaded`, `pack.generation_refused`, `pack.merged`,
`pack.note_drafted`, and the existing `template.rendered` path (proposed #228).
All reach the existing single-writer ledger; no package-local audit writer.

### 1.7 T-01 structured input contract

Add `packs/motor/approval_pack/note.yaml` and activate T-01 in
`packs/motor/templates/registry.yaml` with `body_ref: templates/T-01.j2`,
`status: live`, `channel: pdf`, `min_verification: human_verified`, and active
`required_fields` exactly:

```yaml
- vehicle.reg
- loss.date
- loss.location
- loss.narrative
- assessment.estimate_total
- assessment.agreed_quote
- assessment.pav
- policy.sum_insured
- policy.excess_amount
- policy.excess_protector
```

Do not add speculative claim fields merely to fill the PRD's unresolved slots.
`note.yaml` defines the three ordered classes and exact launch posture:

**Computed/merged, all `locked: true`:**

|slot|source at launch|behaviour|
|---|---|---|
|`amount_payable`|C-08|`PENDING CAPTURE`, blocker `C-08`, no sign|
|`repair_amount`|uncaptured|`BLOCKED ON INPUTS`, no number|
|`assessed_amount`|`assessment.agreed_quote`|render exact cents + citation|
|`estimate`|`assessment.estimate_total`|render exact cents + citation|
|`excess`|`policy.excess_amount`|render exact cents + citation|
|`pav`|`assessment.pav`|render exact cents + citation|
|`percent_si`|formula/rounding uncaptured|`BLOCKED ON INPUTS`, no number|
|`percent_pav`|formula/rounding uncaptured|`BLOCKED ON INPUTS`, no number|
|`garage`|source/display contract uncaptured|`BLOCKED ON INPUTS`|
|`loss_location`|`loss.location`|render exact value + citation|
|`third_party_count`|uncaptured|`BLOCKED ON INPUTS`|
|`excess_protector`|`policy.excess_protector`|render exact bool + citation|
|`duty_paid`|uncaptured|`BLOCKED ON INPUTS`|
|`recovery_register_flag`|uncaptured|`BLOCKED ON INPUTS`|
|`subrogation`|bool+basis uncaptured|`BLOCKED ON INPUTS`|

This is the ED-11 “build the slot, never invent the value” posture (proposed
#229). Only C-08 uses the PRD-mandated text `PENDING CAPTURE`; other unresolved
slots use structured `{state: blocked_on_inputs, blocker: <register ref>}` and
render visible `BLOCKED ON INPUTS`, never a synthetic false/zero. The draft root
is `signable: false` while any such blocker exists.

Every active figure stores `{value, value_type, display, source_ref,
citation_marker}`. Money `value` is the exact integer KES cents from the current
field/calc row; formatting occurs separately and never round-trips through a
float. `source_ref` is the current claim-field id plus its resolved provenance,
or calc-run id for a calc. Missing provenance is a hard
`NOTE_INPUTS_INVALID`, not an unlinked marker (PRD-01 §1.4).

**Verification, all `locked: true`:** ordered slots are
`driver_age_experience`, `driver_is_insured`, `logbook_verification`,
`narrative_photo_consistency`. Use persisted structured CC-2, party-match,
CC-1/CC-4 and CC-5 results only. Do not parse numbers out of rationale prose or
recompute a missing check. Absent evidence renders a structured
`blocked_on_inputs`; `CC-5 flagged` is copied verbatim as `flagged` and cannot be
normalised to pass. The exact driver-party-match producer is uncaptured, so that
slot is blocked at launch (proposed #230).

**Commentary, `locked: false`:** exactly three ordered slots:
`incident_summary`, `excess_vs_max`, `savings_narrative`. No additional free
paragraph, conclusion, liability opinion, or recommendation.

### 1.8 Commentary model, post-processor, and G-NOTE

Add prompt/config `packs/motor/approval_pack/commentary.yaml`:

- task `pack_note_commentary`;
- tier `MODEL_HEAVY`;
- prompt ref exactly `pack.note_commentary@v1`;
- input/output token budgets and max USD are pack data, within PRD-03's
  note-commentary ceiling;
- British English; incident summary maximum 80 whitespace-delimited words;
- no liability adjectives/assertions;
- sections exactly the three ids above;
- use no number absent from supplied `allowed_numbers`.

Call only through the existing AR-4 `ModelWrapper` after the
`pack.note_draft` agent run starts. The model input is canonical JSON containing
only:

1. the ten active fields at `human_verified`, with provenance refs;
2. structured persisted CC results selected for §1.7;
3. `savings_ledger` rows with integer cents and citation evidence; and
4. display-safe allowed-number tokens derived deterministically from those
   values.

No whole claim, document text, email body, party contact, draft HTML, unverified
field, or extracted-not-committed value enters the prompt. Model-call audit is
redacted and spend-governed exactly as AR-4 requires.

Structured output is:

```json
{
  "paragraphs": [
    {
      "template_slot": "incident_summary",
      "content": "...",
      "numbers_used": ["2026-07-01"]
    },
    {"template_slot":"excess_vs_max","content":"...","numbers_used":[]},
    {"template_slot":"savings_narrative","content":"...","numbers_used":[]}
  ]
}
```

`additionalProperties=false`; exact order and ids; strings only. The
post-processor independently tokenises every numeric occurrence in `content`,
requires the multiset to equal `numbers_used`, then requires each token to exist
in the input's allowed-number multiset. It also checks the 80-word cap, forbidden
liability vocabulary from pack config, British-English spelling assertions that
can be deterministic, and exact section count. First failure triggers **one**
fresh governed regeneration with validation errors only; second failure creates
`EXCEPTION{subtype: note_commentary_invalid}` (four-part contract), creates no
note draft, and leaves the claim `RESERVED`. Never repair prose or delete an
unsupported number in code (proposed #231).

Upgrade G-NOTE from its current field-only draft to the PRD-08 producer contract
(proposed #232):

- grade only the three rendered commentary nodes of T-01, excluding cover/page
  numbers, citation labels, structured `numbers_used`, and computed sections;
- the MODEL_HEAVY response identifies every numeric token and a source kind/ref;
- deterministic verification independently reloads the referenced current
  claim field, calc run, or immutable savings-ledger row and confirms the exact
  display token; model omission is caught by complete regex-multiset equality;
- unsupported assertions, missing sections, liability tone, source mismatch, or
  any injected number is `fail`;
- required section ids become the exact three ids; rubric is `T-01@1`, no longer
  `pending_capture`.

G-TPL remains critical and checks active required fields/floor,
StrictUndefined leakage and `PENDING CAPTURE ⇒ signable=false`. Extend it only as
needed to recognise the structured T-01 review artifact; do not weaken its
existing behaviour for other templates.

### 1.9 Draft persistence, grading, review hand-off, FSM

`note_drafts.body` is canonical JSON:

```json
{
  "schema_version": 1,
  "template_id": "T-01",
  "template_version": "1.0.0",
  "merged_pack": {"version": 1, "event_id": "...", "sha256": "..."},
  "sections": [
    {"template_slot": "computed", "content": ["<slot objects>"], "locked": true},
    {"template_slot": "verification", "content": ["<slot objects>"], "locked": true},
    {"template_slot": "incident_summary", "content": "...", "locked": false,
     "numbers_used": []},
    {"template_slot": "excess_vs_max", "content": "...", "locked": false,
     "numbers_used": []},
    {"template_slot": "savings_narrative", "content": "...", "locked": false,
     "numbers_used": []}
  ],
  "blockers": ["<ordered structured blockers>"],
  "signable": false,
  "integrity": {
    "g_tpl_run_id": "...",
    "g_note_run_id": "...",
    "g_tpl_result": "pass",
    "g_note_result": "pass"
  }
}
```

The `sections[]` requirement from the binding DDL is preserved; root metadata is
inside `body`, not new columns. Render `templates/T-01.j2` under
`StrictUndefined` to a deterministic review HTML artifact, store immutably, and
emit `template.rendered{template_id:T-01, note_draft_candidate_id,
merged_pack_event_id, blob_key, signable:false}`.

Before exposing the draft, synchronously run G-TPL and G-NOTE through the public
eval-harness API. Candidate bytes and grader runs remain durable for audit. If
either result is fail/error, persist a `note_drafts` row with `status='draft'`,
record the integrity result/blocker, create one idempotent
`EXCEPTION{subtype: note_integrity_failed}`, create **no NOTE_REVIEW**, and leave
the claim `RESERVED`. This is intentionally stricter than a major grader's
promotion semantics because a known-wrong approval note cannot enter human
signing (proposed #233).

On both passes:

1. insert version 1 (or next version on explicit regeneration) with
   `status='in_review'`, `edited_by/signed_by/signed_at=NULL`;
2. if regenerating, mark the prior unsigned `draft|in_review` row
   `superseded`; never supersede a signed row;
3. emit `pack.note_drafted` with refs/digests only;
4. create exactly one open `NOTE_REVIEW{subtype: approval_note}` whose payload
   references `note_draft_id`, `note_version`, `merged_pack_event_id`, merged
   blob key/digest, review-artifact blob key, blockers, signable=false and grader
   run ids; and
5. transition `RESERVED→PACK_READY` through the canonical FSM.

PRD-00 is earlier and explicit: manifest complete + note drafted owns
`RESERVED→PACK_READY`; officer signature later owns
`PACK_READY→IN_APPROVAL`. PRD-08's compressed phrase “Sign ... → PACK_READY →
IN_APPROVAL” cannot postpone both hops to signature (proposed #234). Packet 19
will implement NOTE_REVIEW editing/signing and the second hop. This packet's
resolution guard returns `409 NOTE_REVIEW_UI_NOT_BUILT` for approve,
edit_approve, or reject and leaves item/draft untouched.

### 1.10 Capabilities and pack/runtime wiring

`packs/motor/autonomy/policies.yaml` becomes explicit:

```yaml
- {id: pack.merge, max_level: L4, initial_level: L1, policy: {}}
- {id: pack.note_draft, max_level: L3, initial_level: L1, policy: {}}
- {id: pack.route, max_level: L4, initial_level: L1, policy: {}}
```

`pack.route` stays uncalled until Packet 19. It is config-only here so no
approval-authority operation is smuggled through it. `pack.note_draft` L3 means
draft + auto-queue only; signature is a human action, never an executor and
never L4 (guide invariant 11).

Add recoverable COP step definitions:

```yaml
- capability_id: pack.merge
  steps:
    - {id: resolve_manifest, expects_events: [pack.merge_requested], produces: [pack.merged]}
- capability_id: pack.note_draft
  steps:
    - {id: generate_commentary, expects_events: [pack.merged], produces: [model.called]}
    - {id: grade_and_queue, expects_events: [model.called], produces: [pack.note_drafted]}
```

The actual step state/output must reflect failures and waits; do not emit a
listed success event merely to satisfy the definition. All side effects pass
through `execute_or_stage`; only immutable local computation occurs inside the
approved executor.

Register a critical grader mapping for merged pack output (deterministic
manifest/digest/source/page checks) under existing G-TPL rather than adding a
tenth grader. T-01 receives G-TPL critical + G-NOTE major. Every new OutputType
has ≥1 critical grader before pack publishability (ED-7a).

## 2. Explicitly out / visibly blocked

- React readiness card and note workspace; inline PDF viewer; citation click UI.
- Commentary editing, diff tracking, append-version autosave, browser-crash
  recovery and “≤5 seconds loss” acceptance.
- Human signature, final signed PDF, `PACK_READY→IN_APPROVAL`.
- Authority route execution, PACK_REVIEW manager queue, approve/reject,
  rejection-preseeded draft, and production-correction capture.
- T-03's 16-field body/recipients and >4M send. PRD-02 routes 4.1M to chairman
  while PRD-08 acceptance says MD + T-03; earlier PRD-02 wins until the owner
  resolves the acceptance contradiction (proposed #235). Packet 19 must not
  silently choose.
- ICON paste-assist field set and all PRD-09 adapter transport.
- Live Chromium/container and S3 Object Lock infrastructure certification
  (#30/#116); injectable production-shaped seams ship now.
- C-08, repair amount, percentage formulas/rounding, garage display source,
  third-party count, duty-paid, recovery and subrogation sources (#5/#229).
- Driver-is-insured producer and any missing CC structured evidence (#230).
- Waivers for any of the 13 manifest items (#222).
- Live projection readback for items 12–13 (#208/#224); upload fallback is live.
- PRD-08 timed generation/review trial and approver-side visual sign-off; these
  are Packet 19/live-trial gates, not claims that CI can manufacture.

## 3. Protected acceptance contract

### 3.1 Readiness response

`GET /claims/{id}/approval-pack/readiness` returns:

```json
{
  "claim_id": "...",
  "status": "RESERVED",
  "ready": false,
  "fingerprint": "<sha256>",
  "checklists": {"ready": true, "blockers": []},
  "fields": {"ready": false, "blockers": [
    {"path":"loss.location","code":"under_verified","required":"human_verified"}
  ]},
  "items": [
    {
      "id": "policy_document",
      "order": 1,
      "label": "Policy document/schedule",
      "state": "ready|missing|ambiguous|invalid|pending_integration",
      "required": true,
      "waivable": false,
      "sources": [{"kind":"document","id":"...","filename":"...",
                   "received_at":"...","sha256":"..."}],
      "blockers": []
    }
  ],
  "blockers": [{"code":"...","item_id":"...","detail":"..."}]
}
```

Exactly 13 ordered items. Blocker ordering is stable: FSM, checklist, item order,
field path, exception. Fingerprint changes if any material source/field/checklist
input changes and is stable otherwise.

### 3.2 Generation responses

- Not ready/stale: 409, structured card included, no artifact/note side effect.
- L1: 202 `{status: staged, capability_id: pack.merge, review_item_id}`.
- Immediate merge with note draft staged: 201
  `{status: merged, pack_version, pack_event_id, note_status: staged,
  note_review_item_id}`.
- Both L3: 201 `{status: ready_for_note_review, pack_version, pack_event_id,
  note_status: in_review, note_draft_id, note_version, note_review_item_id}`.
- Same Idempotency-Key repeats the same status/ids; new key creates the next
  versions.

### 3.3 Acceptance scenarios in this packet

The protected suite pins:

1. **Pack/config/schema:** exact 13 manifest rows; capability levels; T-01 active
   fields/floor; prompt contract; migration exact columns/checks; no new review
   enum; all pack refs validate.
2. **Readiness fail-closed:** non-RESERVED state, missing checklist, open chase,
   missing/ambiguous source, rejected/cross-claim source, invalid PDF, missing or
   extracted-only required field, missing blob and open source exception each
   make `ready=false`; no resolver guesses. An explicit valid source event and
   human verification clear only their own blockers. Items 12/13 never waive.
3. **Reference merged pack (PRD-08 acc. 1):** 13 items, multiple photos and mixed
   supplier quote sources; cover first; exactly 13 correctly-targeted bookmarks;
   item/source/page order exact; photo pages are 2-up with required captions;
   filename exact; event manifest complete; final digest equals stored bytes;
   one ledger row after dispatcher drain.
4. **Missing required source (acc. 2):** remove one resolution before generation;
   409 names its id, no `pack.merged`, no note row. Ambiguous duplicate likewise
   refuses until explicit selection.
5. **HTML safety/fallback:** injected renderer receives the binding offline
   policy; remote URL is never fetched; timeout/crash invokes plaintext once,
   marks only that source's manifest fallback fields, and still completes. Bad
   archive bytes fail rather than blank-render.
6. **Determinism/versioning:** fixed-clock identical builds are byte-equal; new
   request key creates v2 and retains v1; same key is a true replay; changing
   only timestamp is stable under the specified normaliser; different source
   changes digest/fingerprint.
7. **AR-2:** at L1 no renderer/store/model call occurs before DRAFT_RELEASE
   approval; merge release then stages note independently. L3 executes and queues
   without a request click. `pack.note_draft` cannot exceed L3; no signature or
   route event exists.
8. **T-01 exactness (acc. 3):** computed values are integer-identical to current
   field/calc rows and carry resolved source refs; blocked slots carry no number;
   computed/verification sections locked; commentary unlocked; CC-5 flag copied;
   C-08 says `PENDING CAPTURE`; root `signable=false`; no float.
9. **Model integrity:** model input contains only the allowlisted verified bundle,
   CC rows and cited savings rows; task/tier/prompt/budgets are pack-driven. One
   invalid response causes exactly one regeneration; a second produces the
   four-part exception and no review item/FSM hop.
10. **G-NOTE red team (acc. 3):** clean candidate passes; a separately stored
    copy with a deliberately injected KES number absent from all fields/calcs/
    savings fails G-NOTE. A fake grader model that omits that token also fails
    the deterministic token-completeness check.
11. **Draft/FSM:** exact DDL body shape; candidate failure stays `draft` and
    `RESERVED`; both grades pass produces `in_review`, one NOTE_REVIEW and
    `PACK_READY`; regeneration supersedes only unsigned older draft, keeps all
    rows/bytes, and creates next version. Every NOTE_REVIEW resolution is 409
    until Packet 19, with zero mutation.
12. **Regression:** all Packet 01–17 tests remain unmodified and green on SQLite
    and PostgreSQL; money/banned-call lints green.

## 4. CTO decisions and proposed ED-11 register entries

The builder appends these with implementation; next free numbers are #219–#235.

- **#219 — package/installer name.** Use `agents/approval_pack_agent`, public
  `build_approval_pack_agent`, shared `claim_core.Base`.
- **#220 — merged-version durable home/concurrency.** Index immutable versions by
  append-only `pack.merged` events; claim lock gives monotonic versions; add no
  table absent from PRD DDL.
- **#221 — manifest YAML schema.** Pin the minimal keys in §1.2; reject extras.
- **#222 — waiver gap.** All 13 items non-waivable until item-specific authority
  and evidence are captured; especially 12/13 remain hard-required.
- **#223 — missing doc taxonomy/communication semantics.** Use explicit
  claim-owned source selection for policy, intimation, engagement and supplier
  quotes; never invent doc types or infer first/nearest email.
- **#224 — projection artifact contract.** Provide a fail-visible resolver seam;
  do not invent the PRD-09 event. Officer PDF upload makes 12/13 live now.
- **#225 — readiness TOCTOU.** Canonical fingerprint is mandatory at generation;
  stale input returns the recomputed card.
- **#226 — Object Lock infrastructure gap.** Use immutable-store protocol and
  write-once local adapter; production publishability blocked until S3 adapter.
- **#227 — byte-stability normalisation.** Fixed-clock build exact; otherwise only
  visible timestamp header may differ; deterministic PDF metadata/ids.
- **#228 — event/action map.** Register and ledger the six pack events named in
  §1.6 through the existing writer.
- **#229 — unresolved class-(a) mappings.** Register slots as
  `blocked_on_inputs`; add no fields/formulas/default false/zero. C-08 alone uses
  its mandated PENDING CAPTURE text.
- **#230 — verification producer gaps.** Render only persisted structured CC
  evidence; driver party-match remains blocked until its producer is captured.
- **#231 — commentary post-processing.** Exact three-paragraph schema, numeric
  multiset validation, one regeneration then EXCEPTION; never silently edit.
- **#232 — G-NOTE source expansion.** Independently validate commentary numbers
  against fields, calc runs, or cited savings rows; scope tokens to commentary.
- **#233 — grader failure release posture.** Persist failed candidate as draft +
  exception, but no NOTE_REVIEW/PACK_READY until both G-TPL and G-NOTE pass.
- **#234 — FSM hop ownership.** Earlier PRD-00 controls: successful draft moves
  RESERVED→PACK_READY; Packet 19 human signature moves PACK_READY→IN_APPROVAL.
- **#235 — 4.1M authority contradiction.** PRD-02 routes >4M to chairman with
  T-03; PRD-08 acceptance says MD + T-03. Preserve PRD-02 precedence and require
  owner resolution in Packet 19; Packet 18 does not route.

Further ambiguity: narrowest safe behaviour + proposed register entry starting
#236. Do not bury a decision in code or a test fixture.

## 5. Builder guardrails

- **Never guess:** zero/one/many source handling is explicit; communication
  semantics and assessment revisions are never timestamp-selected.
- **No provenance, no commit:** every rendered active field has a current field/
  calc id and resolved provenance; commentary receives only cited ledger rows.
- **Money:** integer KES cents in body/model bundle/G-NOTE checks; formatting
  produces display strings only; no float signatures or percentage invention.
- **StrictUndefined:** missing active fields refuse; only registered blocked slots
  render visible blocked markers; no blank cells/pages.
- **AR-2/AR-4:** merge and note each cross their own gate; model uses wrapper,
  budgets and redacted audit; no direct executor or raw client call.
- **Autonomy constitution:** note max L3; signature is always human; approval
  authority is not a capability; `pack.route` cannot approve.
- **Append/retain:** claim fields untouched; note/pack versions retained;
  immutable blobs never overwritten; audit through the one writer.
- **Closed enum:** use NOTE_REVIEW and EXCEPTION subtypes only; no 18th type.
- **Idempotency:** request key, source selection, event consumers, grader trigger
  and NOTE_REVIEW creation are all duplicate-safe.
- **No payment execution:** this package has no adapter/funds-transfer operation.
- **Portal isolation:** no portal/public route; no whitelist change.
- **Config over code:** manifest, render policy, prompt, model budgets, forbidden
  language and active/blocked slot map are pack data.
- No protected Packet 01–17 fixture change. #218 must be fixed on Packet 17 by
  its owner, not worked around here.

## 6. Definition of done (ED-7/ED-7a)

- Protected Packet 18 suite passes unmodified on SQLite and PostgreSQL; all
  Packet 01–17 suites remain green.
- `ruff check .`, money lint, banned-call lint, and full `pytest -q` green.
- ≥80% branch coverage on new `approval_pack_agent` modules; deterministic
  converters, resolver, numeric post-processor and failure paths covered.
- Migration `0014_note_drafts` reviewed on both dialects; upgrade/downgrade and
  exact columns/checks tested.
- OpenAPI generated and includes only the four routes in §§1.2/1.3/1.5 plus
  read-only merged-version/draft reads needed by Packet 19; no sign/route route.
- Runbook: resolve manifest ambiguity, inspect fallback, stale fingerprint,
  failed commentary, failed grader, regenerate safely, immutable-store outage,
  and Packet-19 resolution blocker.
- Grader coverage registered: merged artifact G-TPL critical; T-01 G-TPL
  critical + G-NOTE major; red-team number injection in CI.
- Dependency/container note documents pypdf and the required Chromium pin; live
  Chromium/S3 certification remains an explicit production blocker, not a false
  “done”.
- PR description lists #219–#235, exact files changed, migration review,
  coverage, OpenAPI diff, runbook, and out-of-scope Packet 19 hand-off.

## 7. Packet 19 hand-off contract

Packet 19 consumes, without schema reinterpretation:

- readiness response/fingerprint and 13-item ordering;
- `pack.merged` event payload and immutable PDF read;
- `note_drafts.body`/version/status and T-01 review artifact;
- NOTE_REVIEW payload with exact pack/draft/grader refs;
- blocked `signable=false` posture and integrity runs.

Packet 19 alone may add the editor/autosave/sign/route/approval APIs and UI. It
must resolve #235 before asserting the 4.1M scenario and must retain the hard
rule that no draft containing a blocked slot or failed grader can be signed.
