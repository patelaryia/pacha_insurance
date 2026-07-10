# PACKET-05 — Document Intelligence live stages, split, vision citations, Swahili, consistency (PRD-01 slice 2 of 2)

> **Status:** issued · **Builder:** Codex per `AGENTS.md` · **Reviewer:** CTO per `CLAUDE.md`
> **Source spec:** `docs/PRD-01_Document_Intelligence_Engine_v1.1.md` §1.1–§1.7;
> Section 0 ED-4/ED-4a and ED-11; PRD-04 §4.3 closed review-item enum.
> Precedence: Section 0 → PRD-01 → PRD-04 → this packet.
> **Depends on:** PACKET-04 merged (`claim_core` + deterministic `doc_intel` substrate).
> **Acceptance tests:** `tests/acceptance/test_packet_05_docintel_live_model.py` —
> protected and failing by design until this packet is built.
> **Completion:** this packet completes PRD-01 except the corpus/calibration gates already
> deferred to PRD-03 by register #36.

## 1. Scope

**Packet-04 ratchets (blocking review findings):**

1. Enforce “no provenance, no commit” in the shared `claim_core` write gate, not only
   in `doc_intel.commit`. Any write with `verification_state='extracted'` must be an
   extraction write with a real document on the same claim and a resolved
   `anchor_text` or verified `vision_bbox` citation. Reject with
   `422 CITATION_REQUIRED`; do not create a field row or event.
2. Preserve and pass the email subject as the explicit CLASSIFY input required by
   §1.2. Body text is not a substitute for the subject.
3. Add literal failure-path coverage for a raster-only 40-page scan, photo-only
   intimation, and duplicate attachment dedupe.
4. Keep `licence_no` and `phone_ke` fail-closed (`out_of_scope`) until their formats
   are captured; register the missing formats per ED-11 (#39).
5. Expose every pipeline stage as a named Celery task while retaining the
   synchronous test driver. Tasks call the same idempotent stage service; there is
   no second implementation.

**PRD-01 slice 2:**

1. Real Anthropic `ModelClient` adapter using structured tool-use, model ids and
   pricing from configuration; all pipeline calls continue through `ModelWrapper`.
2. CLASSIFY and EXTRACT live-call contracts, including DOC_CLASSIFY review routing.
3. Mandatory page-level heterogeneity detector, DOC_SPLIT review item, human-boundary
   resolution, child documents with `parent_document_id`, and child re-entry at
   CLASSIFY. No agent-proposed boundaries in v1.1.
4. `vision_bbox` citations, eligibility enforcement, render-crop MODEL_LIGHT
   verification, and the ×0.9 confidence multiplier.
5. Police-abstract Swahili remarks gloss, explicitly marked machine-translated and
   structurally ineligible as a rule input.
6. Pack-defined consistency engine CC-1..CC-5, append-only
   `consistency_results`, and CONSISTENCY_FLAG review events.
7. Per-document duration/cost samples plus fail-loud SLO alerts for the specified
   three-minute and US$0.60 sentinels.

**Out:** the §1.7 corpus accuracy gates and monthly calibration job (PRD-03,
register #36), splitter boundary proposals (`intake.doc_split`, deferred by the
PRD), citation-viewer UI (PRD-04), and production review-item persistence/workspaces
(PRD-04). Packet 05 emits only closed-enum review types.

## 2. Binding spec quotations (verbatim)

PRD-01 §1.1:

> “Single shared pipeline: any inbound artifact → classified, parsed, extracted into
> `claim_fields` with citations and calibrated confidence. Agents never call LLMs on
> documents directly; they call this engine.”

PRD-01 §1.2, CLASSIFY/SPLIT/EXTRACT through CONSISTENCY:

> “→ CLASSIFY    MODEL_LIGHT, input: page-1 image + first 2k chars + filename + email
>                subject; output: {doc_type ∈ pack taxonomy ∪ 'other', confidence}.
>                <0.80 → review item DOC_CLASSIFY; 'other' → review item always
>  → SPLIT?      heterogeneity detector (v1.0, mandatory): page-level MODEL_LIGHT
>                classification runs when doc-level classify returns 'other'/low-conf
>                OR page_count > 4 with mixed page classes → review item DOC_SPLIT:
>                thumbnail-strip UI, officer draws boundaries; children created as
>                documents with parent_document_id, each re-entering the pipeline
>                from CLASSIFY. Human boundaries never misclassify. v1.1 (deferred):
>                agent pre-fills proposed boundaries in the same UI (capability
>                intake.doc_split, starts L1). The splitter contract, child-document
>                schema, and DOC_SPLIT item are built NOW; only the proposal is later.
>  → EXTRACT     MODEL_HEAVY per doc_type schema (below); structured tool-use output
>  → CITE        anchor-match each field (1.4) → bbox provenance or citation_failed
>  → VALIDATE    deterministic validators (1.5) → combined confidence
>  → COMMIT      write claim_fields batch (source_type='extraction'); below-threshold
>                fields → review item FIELD_VERIFY; emit document.extracted
>  → CONSISTENCY cross-document rules (1.6) when trigger sets complete”

PRD-01 §1.2, budgets:

> “Budgets: p95 ≤ 3 min/document end-to-end; cost ceiling US$0.60/document p95
> (alert at breach — this is your gross-margin sentinel).”

PRD-01 §1.4, vision citations and the commit invariant:

> “**Mode 2 — `vision_bbox` (v1.1):** the model returns normalized bbox coordinates
> directly. Permitted **only** for doc types flagged `handwritten: true` in the pack,
> or pages with < 5% text coverage. Verification reuses G-CITE's render-crop check
> (crop the claimed bbox, MODEL_LIGHT confirms the value is visible in the crop).
> Vision-cited fields carry a **×0.9 confidence multiplier** — stated consequence:
> most handwritten claim-form fields will land in `FIELD_VERIFY` at launch (~10
> fields × ≤10s ≈ under 2 minutes per claim form — an accepted, sized review load;
> the corpus calibrates from there).
>
> **A field cannot reach `verification_state='extracted'` without a resolved citation
> (either mode).**”

PRD-01 §1.4, Swahili content:

> “**Swahili content (police abstracts):** structured fields (OB number, station,
> dates) are format-based and in scope for extraction. The remarks field is captured
> verbatim, plus a machine-translation gloss stored as a derived field flagged
> `machine_translated: true` — narrative-only, **never a rule input**. The console
> renders value ↔ highlighted region side-by-side (PRD-04); the grader re-checks
> citation-value consistency (PRD-03).”

PRD-01 §1.5:

> “`combined = model_conf × V` where V: named validator pass = 1.0; fail = 0.30;
> not applicable = 0.95. Thresholds (defaults, overridable per field in schema):
> money/date/registration fields **0.90**, all else **0.85**. Below threshold →
> `FIELD_VERIFY` review item (field, value, citation viewer, accept/correct).
> Calibration job (PRD-03) recomputes reliability curves monthly from human
> corrections; thresholds are data, not code.”

PRD-01 §1.6:

> “Pack-defined checks, each `{id, trigger_docs[], expression, severity}` running when
> its trigger set is present. Motor v1: `CC-1` reg match
> logbook=claim=estimate=assessor_report (severity: block-pack); `CC-2` DL
> dob→computed age vs claim_form driver age ±1yr (flag); `CC-3` DL expiry ≥ loss date
> (block-pack); `CC-4` owner_name(logbook) ≈ insured_name (fuzzy ≥0.8, flag); `CC-5`
> **narrative-vs-photos**: MODEL_HEAVY vision compares `loss.narrative` against all
> `photo_damage` descriptions → `{consistent|inconsistent|insufficient, rationale,
> score}` — **always emits a review flag when not consistent, never blocks, never
> auto-clears** (D-01 boundary, hard-coded: this check type is not eligible for
> autonomy above L2). Results stored in `consistency_results`, surfaced on the claim
> and injected into the approval note's verification section.”

PRD-01 §1.7:

> “Against the PRD-03 motor corpus: classification accuracy ≥ 97%; financial-field
> extraction ≥ 98%, all fields ≥ 95%; citation resolution ≥ 99% of committed fields;
> handwritten claim-form per-field accuracy baseline measured in week 1 and threshold
> set with Claims Manager (A-4/Q-07 discharge); zero fields committed without
> provenance. Failure-path tests: corrupt PDF, 40-page scan, XLSX estimate, photo-only
> intimation, duplicate attachment (sha dedupe).”

Section 0 ED-4:

> “Model IDs live in config, never in code — swapping models must be a config change.
> All calls: structured output via tool-use JSON schemas, `temperature=0` for
> extraction/rules paths, request/response logged (with PII field-level redaction
> rules from ED-6) to the audit ledger.”

Section 0 ED-4a:

> “Launch config values: `MODEL_HEAVY = claude-sonnet-4-6`, `MODEL_LIGHT =
> claude-haiku-4-5-20251001`. Each tier carries a `fallback_model_id` (the previous
> pinned version of the same tier).”

PRD-04 §4.3:

> “Review-item type enum — FINAL AND CLOSED (v1.1). Do not add types; new cases are
> `EXCEPTION` subtypes:
> `FIELD_VERIFY, DOC_CLASSIFY, DOC_SPLIT, CONSISTENCY_FLAG, DRAFT_RELEASE,
> MODE_CONFIRM, NOTE_REVIEW, PACK_REVIEW, EX_GRATIA, EXCEPTION, PROMOTION_SIGNOFF,
> SAMPLE_REVIEW, PASTE_READBACK_CHECK, PROCEED_PARTIAL, KYC_VERIFY, EFT_MATCH,
> REOPEN_PROMPT`”

## 3. Deliverables and pinned interfaces

### 3.1 Files and storage

```text
platform/doc_intel/
  anthropic_client.py  # real structured tool-use adapter; injected SDK client in tests
  split.py             # detector + human boundary application
  vision.py            # eligibility, normalized bbox validation, crop verification
  swahili.py           # derived remarks-gloss artifact
  consistency.py       # config-driven CC-1..CC-5 engine
  telemetry.py         # per-document SLO samples + AlertSink
  tasks.py             # one named Celery task per stage
packs/motor/doc_intel.yaml       # model tiers, pricing, thresholds, SLO config
packs/motor/consistency.yaml     # CC-1..CC-5 definitions as data
```

Alembic migration `0004_docintel_live_stages` adds:

- `documents.parent_document_id TEXT NULL REFERENCES documents(id)`.
- append-only `consistency_results(id TEXT PK, claim_id TEXT FK, check_id TEXT,
  status TEXT, severity TEXT, rationale TEXT, score NUMERIC(4,3), evidence JSON,
  created_at TIMESTAMPTZ)`; no update path is exposed.
- append-only `doc_intel_samples(id TEXT PK, document_id TEXT FK, duration_ms BIGINT,
  cost_usd NUMERIC(8,6), breached_duration BOOL, breached_cost BOOL, created_at
  TIMESTAMPTZ)`.

The locally designed DDL is registered under #41/#44. It is the narrowest schema
that satisfies the PRD and remains migration-compatible with later analytics.

### 3.2 Shared provenance gate

`ClaimService._validate_write` enforces for every `verification_state='extracted'`:

- `source_type == 'extraction'`;
- `source_ref.document_id` exists and belongs to the claim;
- `page` is a positive integer and bbox is four finite normalized coordinates;
- exactly one resolved mode is present: non-empty `anchor_text` (≤120 chars), or
  `citation_mode='vision_bbox'` plus `vision_verified=true`.

The validation requiring database ownership runs inside the same claim transaction
as the append-only write. The generic PATCH route cannot bypass it.

### 3.3 Model adapter and call contracts

`AnthropicModelClient(sdk_client, *, config, ledger)` implements the existing
`ModelClient` protocol. It calls `messages.create` with `temperature=0`, one tool
whose `input_schema` is the requested schema, and `tool_choice` forcing that tool.
Tier→model id and token pricing come only from `packs/motor/doc_intel.yaml`.
Provider usage is converted to `cost_usd` from config; request/response audit detail
is redacted before ledger append. HTTP 429/5xx/timeouts are translated to
`ModelTransportError`; no retry exists inside the adapter.

All model inputs carry a stable `task` value:
`document_classify | page_classify | extract | vision_crop_verify |
translate_swahili_gloss | consistency_cc5`.

### 3.4 Split contract

Page-level classification runs when document classification is `other`, below 0.80,
or the document has more than four pages. Running it for every >4-page document is
the safe way to determine whether page classes are mixed (register #40). Mixed,
low-confidence, or `other` page results create one idempotent DOC_SPLIT item and
pause the parent before EXTRACT. Homogeneous high-confidence pages let it continue.

```python
engine.apply_human_boundaries(
    parent_document_id,
    boundaries=[{"start_page": 1, "end_page": 2}, ...],
    actor="user:<ulid>",
) -> list[str]  # child document ids
```

Boundaries must be contiguous, non-overlapping, cover every parent page exactly
once, and contain at least two children; invalid input is `422 INVALID_SPLIT_BOUNDARY`.
Each child is an immutable PDF subset, sets `parent_document_id`, records its page
range in `source`, emits `document.received`, and starts with CLASSIFY pending.
No proposed doc type is copied from the page model: human boundaries never
misclassify. Reapplying the same resolution is idempotent.

### 3.5 Vision and Swahili contracts

EXTRACT fields use a discriminated citation union:

- `{"citation_mode":"anchor_text", "anchor_text":str, "page":int, ...}`
- `{"citation_mode":"vision_bbox", "bbox":[x0,y0,x1,y1], "page":int, ...}`

Vision mode is rejected to FIELD_VERIFY unless the schema is handwritten or the
normalized page artifact records text coverage `<0.05`. Eligible bboxes are cropped
from the immutable 300dpi page and verified by MODEL_LIGHT. Only `visible=true`
sets `vision_verified=true`; false/invalid responses force confidence 0 and review.
The confidence calculation is `model_conf × V × 0.9` for vision fields.

Because PRD-01 supplies no canonical claim-field path for the Swahili gloss, Packet
05 stores `derived/{document_id}/remarks_gloss.json` with
`{source_field:'remarks', value, machine_translated:true, rule_input:false,
status:'pending_field_registration'}` (register #42). It is visible and durable but
cannot enter `claim_fields` or any rule expression until the path is captured.

### 3.6 Consistency engine

Definitions load strictly from `packs/motor/consistency.yaml`. Missing trigger docs
produce no result. A completed trigger set writes one append-only result per input
fingerprint; repeated delivery is idempotent.

- CC-1/3/4 implement the exact expressions and severities above.
- CC-2 is always `insufficient` + CONSISTENCY_FLAG until the undefined “claim_form
  driver age” source is captured (register #43); `years_driving` is never substituted.
- CC-5 uses MODEL_HEAVY, creates CONSISTENCY_FLAG whenever status is not
  `consistent`, never creates a block, never auto-clears a prior flag, and exposes
  the hard maximum autonomy level `L2`.

`engine.consistency.evaluate_claim(claim_id)` reads document-scoped extraction
artifacts as well as canonical fields; it never relies on the current `vehicle.reg`
alone when comparing registrations from four documents.

### 3.7 SLO sentinel

Every terminal document attempt writes one sample. Exact p95 aggregation window is
not specified (register #44), so the safe launch behaviour is stricter: alert on
each individual duration `>180_000ms` or cost `>0.60`, while persisting samples for
the later PRD-03 p95 series. `AlertSink.alert(code, payload)` receives
`DOC_INTEL_DURATION_BREACH` or `DOC_INTEL_COST_BREACH`; no new event or review-item
type is invented.

## 4. CTO decisions and ED-11 register entries

- **#39:** `licence_no` and `phone_ke` formats are absent; retain `out_of_scope` and
  review for every non-empty value until captured.
- **#40:** the >4-page mixed-class condition requires page classifications before
  mixedness is knowable; classify every page for all >4-page documents.
- **#41:** `consistency_results` DDL is absent; use the minimal append-only schema in
  §3.1.
- **#42:** no canonical Swahili-gloss field path is specified; store a visible
  derived artifact with `pending_field_registration`, never a rule input.
- **#43:** CC-2 references claim-form driver age, but the schema has DOB and
  years-driving only; emit `insufficient`, never substitute another value.
- **#44:** SLO p95 window is unspecified; persist every sample and alert on each
  individual breach until the PRD-03 aggregation contract lands.
- **#45:** DOC_SPLIT resolution transport is unspecified before PRD-04; expose the
  engine method in §3.4, with the future PRD-04 resolver calling that same method.

## 5. Builder guardrails

- Do not modify protected Packet-04/05 acceptance tests.
- No direct model SDK use outside `anthropic_client.py`; no document agent calls the
  provider directly.
- No `ClaimField(` or in-place claim-field updates outside `claim_core`.
- Zero provenance means zero commit at the shared gate, including generic APIs.
- Model ids, prices, thresholds, rule expressions, and SLO values are configuration.
- No splitter proposal/autonomy implementation in this packet.
- CC-5 is constitutionally capped at L2; consistency flags never auto-clear.
- Only the 17 PRD-04 review types may be emitted.

## 6. Definition of done

- Packet-04 acceptance remains green; Packet-05 acceptance passes unmodified on
  SQLite and PostgreSQL CI legs.
- Unit coverage ≥80% on all `platform/doc_intel/` engine code.
- Migration 0004 reviewed; OpenAPI regenerated if the shared PATCH error contract
  changes.
- Runbook covers provider outage, split-review backlog, vision verification failure,
  and SLO alerts.
- PRD-03 `grader_map.yaml` entries are prepared for every new OutputType when the
  eval harness lands; register #36 remains visibly open until then.
- `ruff check .`, money lint, banned-calls lint, and full pytest pass.
