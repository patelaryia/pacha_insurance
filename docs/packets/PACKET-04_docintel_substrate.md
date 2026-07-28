---
id: PACKET-04
prd_ref: "docs/PRD-01_Document_Intelligence_Engine_v1.1.md \xA71.2\u2013\xA71.5 (+\xA71.7\
  \ failure paths); Section 0 ED-4/ED-4a (LLM wrapper contract), ED-8, ED-9. Precedence: Section\
  \ 0 \u2192 PRD-01 \u2192 this packet."
title: 'Document Intelligence substrate: pipeline, normalize, schemas, validators, citations,
  commit (PRD-01 slice 1 of 2)'
depends_on:
- PACKET-03
status: merged
branch: codex/packet-04-docintel-substrate
attempts: 0
blast_radius: true
acceptance_tests:
- tests/acceptance/test_packet_04_docintel_substrate.py
review_findings: []
merged_at: '2026-07-22T00:00:00Z'
pr: (pre-loop; see git log)
---

# PACKET-04 — Document Intelligence substrate: pipeline, normalize, schemas, validators, citations, commit (PRD-01 slice 1 of 2)

> **Status:** issued · **Builder:** Codex per `AGENTS.md` · **Reviewer:** CTO per `CLAUDE.md`
> **Source spec:** `docs/PRD-01_Document_Intelligence_Engine_v1.1.md` §1.2–§1.5 (+§1.7
> failure paths); Section 0 ED-4/ED-4a (LLM wrapper contract), ED-8, ED-9.
> Precedence: Section 0 → PRD-01 → this packet.
> **Depends on:** PRD-00 complete on main (`claim_core`).
> **Acceptance tests:** `tests/acceptance/test_packet_04_docintel_substrate.py` —
> protected, failing by design.
> **Packet 5 (next):** live model stages — CLASSIFY, SPLIT, EXTRACT (real), vision_bbox,
> Swahili, consistency engine CC-1..5, cost sentinel with the real Anthropic client.

## 1. Scope

**In:** everything in PRD-01 that runs **without a model call** —
1. Resumable pipeline stage framework (each stage a Celery-wrappable callable,
   per-document stage state, event-driven entry from `document.received`).
2. NORMALIZE: eml/msg→PDF, images→PDF, XLSX native + CSV snapshot, 300dpi page
   renders (ED-9 lifecycle noted), PyMuPDF text layer with word bboxes, OCR fallback
   hook (<5% text coverage), corrupt-input failure path.
3. Extraction **schema registry** — pack-registered JSON-schema-shaped definitions,
   prompt text generated from the schema; all 11 motor doc-type schemas as data.
4. All §1.3 named validators, exactly.
5. Citation mode 1 — `anchor_text` fuzzy match → bbox (§1.4 algorithm, verbatim).
6. Confidence model §1.5 + COMMIT stage: batch write through `claim_core`'s public
   write path, `FIELD_VERIFY` review slot, **zero provenance ⇒ zero commit**.
7. LLM wrapper **interface** per ED-4a (retry/fallback/budget/failure taxonomy) with
   deterministic fake clients — packet 5 plugs the real Anthropic client into this seam.
8. Dictionary pack-extension mechanism + interim motor field extensions
   (`packs/motor/fields.yaml` — the first `/packs` artifact).

**Out (packet 5):** real CLASSIFY/EXTRACT/SPLIT-detector calls, `vision_bbox` mode +
crop check, ×0.9 multiplier, Swahili gloss, consistency engine (CC-1..CC-5,
`consistency_results` table), $0.60/doc + 3min p95 sentinels, DOC_CLASSIFY/DOC_SPLIT
review flows (their *slots* exist as stage plug points now).
**Out (elsewhere):** §1.7 corpus accuracy gates (97/98/95/99%) — not CI-computable
until the corpus (open item 7) and the PRD-03 harness exist (register #36); monthly
calibration job (PRD-03); console citation viewer (PRD-04).

## 2. Binding spec quotes (implement verbatim)

PRD-01 §1.2, pipeline:

> "`document.received` → NORMALIZE msg/eml→PDF via rendering; images→PDF; XLSX kept
> native+CSV snapshot; render every PDF page to PNG @300dpi (S3:
> `pages/{doc_id}/{n}.png` — deleted at 180d, regenerated on demand, per ED-9);
> extract native text layer w/ word bboxes (PyMuPDF); OCR fallback (Tesseract 5) when
> text coverage <5% of page area; sha256 dedupe → CLASSIFY … → EXTRACT … → CITE
> anchor-match each field (1.4) → bbox provenance or citation_failed → VALIDATE
> deterministic validators (1.5) → combined confidence → COMMIT write claim_fields
> batch (source_type='extraction'); below-threshold fields → review item FIELD_VERIFY;
> emit document.extracted"

PRD-01 §1.3, registry:

> "Per `doc_type`, packs register a JSON Schema where every leaf carries: `type`,
> `required`, `validator` (named, see 1.5), `pii_class`, `confidence_threshold`,
> `target_path` (claim_fields path). The extraction prompt is **generated from the
> schema** … adding a medical invoice later = registering a schema, no prompt
> engineering project."

PRD-01 §1.3, motor schemas: the 11-row table is the spec — implement every doc_type
and field list exactly (`intimation_email`, `claim_form`, `logbook`,
`driving_licence`, `kra_pin_cert`, `police_abstract`, `repair_estimate`,
`assessor_report`, `discharge_voucher`, `bank_discharge_letter`, `photo_damage`).

PRD-01 §1.3, validators:

> "`kenya_reg` v1.1 pattern set — standard `^K[A-Z]{2} ?\d{3}[A-Z]$`; pre-2000 legacy
> `^K[A-Z]{2} ?\d{3}$`; motorcycles `^KM[A-Z]{2} ?\d{3}[A-Z]$`; trailers
> `^Z[A-Z] ?\d{4}$`; government `^GK ?[A-Z]? ?\d{3}[A-Z]?$`; diplomatic/military/NGO
> plates **out of scope v1** → classify-to-review, never auto-match … `kra_pin`
> (`^[AP]\d{9}[A-Z]$`), `date_past`, `money_kes` (parses shilling-denominated strings
> from documents and multiplies by 100 on commit — storage is integer KES cents per
> ED-8; explicit cents in source documents parsed when present; handles `KES/KSh/,`
> variants), `sum_check` (line items Σ = total, tolerance KES 1), `licence_no`,
> `phone_ke`."

PRD-01 §1.4, citation mode 1:

> "the extraction schema forces the model to return, per field: `{value, anchor_text
> (verbatim ≤120 chars from source), page}`. Post-process: fuzzy-match `anchor_text`
> against that page's word-bbox stream (normalized Levenshtein ≥ 0.85 over a sliding
> window) → derive bbox → store in `source_ref`. Match failure ⇒ `citation_failed:
> true` ⇒ confidence forced to 0 ⇒ review item."
>
> "**A field cannot reach `verification_state='extracted'` without a resolved
> citation (either mode).**"

PRD-01 §1.5, confidence:

> "`combined = model_conf × V` where V: named validator pass = 1.0; fail = 0.30; not
> applicable = 0.95. Thresholds (defaults, overridable per field in schema):
> money/date/registration fields **0.90**, all else **0.85**. Below threshold →
> `FIELD_VERIFY` review item … thresholds are data, not code."

Section 0 ED-4a, wrapper failure taxonomy (binding on the interface built here):

> "- Transport errors / HTTP 429 / 5xx / timeout → silent bounded retry: exponential
> backoff 1s → 60s, max 6 attempts, ≤ 10 min total; switch to `fallback_model_id`
> after attempt 3.
> - Schema-invalid structured output → exactly one regeneration attempt, then
> `EXCEPTION` review item.
> - Budget breach (AR-4 table) → `EXCEPTION{type: budget_exceeded}` immediately, no retry.
> - Provider fully down (retries exhausted) → agent run pauses"

PRD-01 §1.7, failure paths (packet-4 subset):

> "Failure-path tests: corrupt PDF, 40-page scan, XLSX estimate, photo-only
> intimation, duplicate attachment (sha dedupe)."

## 3. Deliverable

New package **`platform/doc_intel/`**, import name `doc_intel` (register #33).
Cross-package access **only** via `claim_core`'s package root exports — add a curated
`claim_core.__init__` public interface (`create_app`, `ClaimService`, `BlobStore`,
event recorder access via `app.state`); `doc_intel` must not import `claim_core.<internal>`
modules directly (ED-1 module-boundary rule).

```
platform/doc_intel/
  engine.py        # DocIntelEngine + build_engine(app, *, model_client, ocr_engine=None)
  stages.py        # stage framework: StageResult, per-doc stage state, resume logic
  normalize.py     # eml/msg/image/xlsx/pdf normalisation, page renders, text layer, OCR hook
  registry.py      # schema registry + prompt generation
  citations.py     # §1.4 mode-1 anchor matcher
  validators.py    # §1.3 named validators
  confidence.py    # §1.5 model + thresholds-as-data
  commit.py        # COMMIT stage → claim_core write path + FIELD_VERIFY slot
  llm.py           # ModelClient protocol + wrapper (ED-4a semantics) + fakes
  schemas/motor/*.yaml   # the 11 doc-type schemas (data)
packs/motor/fields.yaml  # dictionary extensions (data) — first /packs artifact
```

### 3.1 Engine contract (pinned by acceptance tests)

```python
from doc_intel.engine import build_engine
engine = build_engine(app, *, model_client, ocr_engine=None, clock=None)
# - registers consumer "doc_intel" on app.state.dispatcher: document.received
#   → runs the pipeline for that document (synchronously drivable, D-9 posture)
# - engine.process_document(document_id) -> PipelineOutcome  (idempotent, resumable)
# - engine.registry: SchemaRegistry (doc_type -> schema; .prompt_for(doc_type) -> str)
```

`PipelineOutcome`: `{document_id, stages: {stage: status}, committed_paths: [...],
review_items: [{type, ...}], failed: bool}`.

**Pinned surface (acceptance tests rely on exactly these):**
- `app.state.doc_intel` = the engine; `app.state.blob_store` = the claim_core blob
  store (claim_core exposes it on state in this packet).
- `BlobStore` gains `get(key) -> bytes`, `exists(key) -> bool`,
  `list_keys(prefix) -> list[str]` (extend claim_core's protocol + LocalBlobStore).
- Stage⇄model data contracts:
  - CLASSIFY (`tier="MODEL_LIGHT"`) → `data = {"doc_type": str, "confidence": float}`
  - EXTRACT (`tier="MODEL_HEAVY"`) → `data = {"fields": [{"name", "value",
    "anchor_text", "page", "confidence"}]}`
  - `ModelResult` (wrapper return / client return) = `{"data": dict,
    "cost_usd": float, "model_id": str}` (`cost_usd` is telemetry, not Money).
- `ModelWrapper(client, *, budget_ceiling_usd=None, config=None, clock=None)`;
  budget rule: once accumulated spend **≥ ceiling**, every subsequent call raises
  `ModelBudgetExceeded` before any live call.
- Validators return `ValidatorResult` with `.outcome ∈ {pass, fail, not_applicable,
  out_of_scope}` and `.value` (the normalised value, e.g. integer cents for
  `money_kes`).

- Pipeline stage state persisted in new table `document_stages` (register #35):
  `id` ULID, `document_id` FK, `stage` (`NORMALIZE|CLASSIFY|SPLIT|EXTRACT|CITE|VALIDATE|COMMIT|CONSISTENCY`),
  `status` (`pending|succeeded|failed|skipped`), `attempts`, `last_error`, `output_ref`
  (blob key or JSON), timestamps. Re-running a document resumes at the first
  non-succeeded stage.
- CLASSIFY/EXTRACT stages call the injected `model_client` through the ED-4a wrapper —
  packet 4 acceptance uses fakes; the stage plumbing must not care which.
- SPLIT and CONSISTENCY stages: registered in the enum, `status='skipped'` with
  `last_error='packet-05'` until packet 5 (visible slot, no silent absence).
- Corrupt/unreadable input at NORMALIZE: document status → `rejected`,
  `document.rejected` event, `EXCEPTION{doc_normalize_failed}` review event, pipeline
  marked failed — never a silent drop.

### 3.2 NORMALIZE specifics

- Inputs by mime/extension: `application/pdf` passthrough; `message/rfc822`/`.eml`
  and `.msg` (extract-msg) → **plaintext body rendered to PDF via PyMuPDF with a real
  text layer** (D: rich-HTML fidelity deferred, register #34); `image/*` → single-page
  PDF (Pillow); XLSX → kept native + CSV snapshot artifact (`openpyxl`, every sheet)
  + a text-layer PDF of the CSV for citation purposes.
- Page renders: PNG 300dpi to blob store `pages/{doc_id}/{n}.png`; store page count
  on the document row.
- Text layer: per page, word list `[{text, bbox: [x0,y0,x1,y1]}]` persisted as a blob
  artifact (`text/{doc_id}/{n}.json`), normalized page coordinates.
- OCR: `OcrEngine` protocol (`words(page_png_bytes) -> [{text, bbox}]`). Trigger:
  native text coverage `< 5%` of page area. `TesseractOcrEngine` implemented
  (pytesseract) but **injectable** — acceptance tests inject a fake; a unit test
  exercises Tesseract, `skipif` binary absent (CI installs it).

### 3.3 Schema registry + prompt generation

- Schema files as YAML data; every leaf: `type, required, validator, pii_class,
  confidence_threshold (optional override), target_path (optional — fields without a
  target stay document-scoped, e.g. `broker_name`, line_items)`.
- `registry.prompt_for(doc_type)` renders a deterministic prompt: doc-type intro +
  per-field name/type/description/format example + the §1.4 anchor requirement
  (`value, anchor_text ≤120 chars verbatim, page`). Unknown doc_type → raises
  (never guesses).
- Registering a schema whose `target_path` is not in the field dictionary (core +
  pack extensions) → registry refuses at load (StrictUndefined doctrine).

### 3.4 Validators (§1.3 — table-driven, all pure functions)

`kenya_reg` (full pattern set; anything unmatched → validator outcome
`out_of_scope` which forces review, never auto-match), `kra_pin`, `date_past`,
`money_kes` (string parse → **integer cents ×100**, cents parsed when present,
`KES/KSh/,` variants; output feeds the commit value — floats never appear),
`sum_check` (Σ line amounts vs total, tolerance `1_00` cents), `licence_no`,
`phone_ke`. Validator outcomes: `pass | fail | not_applicable | out_of_scope`.

### 3.5 Citation + confidence + commit

- Anchor matcher: normalized Levenshtein ≥ 0.85 (rapidfuzz) over a sliding window of
  the page's word stream; window sized by anchor token count ±2; bbox = union of
  matched words' boxes. `≥` is binding (0.85 exactly passes).
- `combined = model_conf × V`; V per §1.5 (`out_of_scope` maps to fail-style 0.30 —
  and always reviews). Thresholds from schema override else defaults: 0.90 for
  `money|date|registration`-validator fields, 0.85 else. **`combined >= threshold`
  commits** (register #38 pins ≥).
- COMMIT: one batch through `claim_core` public write path (`source_type='extraction'`,
  `verification_state='extracted'`, `confidence=combined`, `source_ref={document_id,
  page, bbox, anchor_text}`). Below-threshold or `citation_failed` fields: **not
  committed**; `review.created` event `{"type": "FIELD_VERIFY", "document_id", "path",
  "candidate_value", "combined_confidence", "citation": ...}` (candidate value lives
  in the review payload — PII-bearing candidates for PII paths carry
  `candidate_value: "__redacted__"` + blob ref instead, keeping event payloads
  PII-clean). Emit `document.extracted` when the batch lands.

### 3.6 LLM wrapper (`llm.py`) — ED-4a semantics, interface-first

```python
class ModelClient(Protocol):
    def structured_call(self, *, tier: str, schema: dict, inputs: dict) -> ModelResult: ...
```

- `ModelWrapper(client, config, clock)` enforces ED-4a: bounded retry w/ backoff
  (1s→60s, max 6, ≤10min, fallback model id after attempt 3 — config data per ED-4),
  schema-invalid → exactly one regeneration then raise `ModelSchemaError` (caller
  emits `EXCEPTION`), budget breach → raise `ModelBudgetExceeded` immediately (caller
  emits `EXCEPTION{budget_exceeded}`), retries-exhausted → `ModelUnavailable`
  (pipeline pauses the document: stage `failed`, resumable).
- Budget accounting slot: per-document running cost accumulator (real pricing table
  is packet 5; the accumulator + ceiling check machinery land now, config data).
- `FakeModelClient(responses)` + `FlakyModelClient(...)` for tests.
- Model ids/tiers are **config data** (ED-4): `MODEL_HEAVY`/`MODEL_LIGHT` +
  `fallback_model_id` from a config mapping, never literals in code.

## 4. CTO decisions (D-x) and register entries

- **Register #33** — PRD-01 package = `platform/doc_intel/`, import `doc_intel`;
  cross-package access only via `claim_core` root exports (ED-1 boundary).
- **Register #34** — email rendering v1 = plaintext→PDF via PyMuPDF (citation-viable
  text layer); rich-HTML fidelity + attachments-in-msg deferred; `.msg` via
  extract-msg.
- **Register #35** — `document_stages` DDL designed locally (PRD gives stages, no DDL).
- **Register #36** — §1.7 corpus accuracy gates (97/98/95/99%) not CI-computable:
  corpus = open item 7, measurement harness = PRD-03. Packet tests cover mechanics +
  failure paths only.
- **Register #37** — motor dictionary extensions shipped as `packs/motor/fields.yaml`
  interim; full pack repo format/signing is PRD-13 (built alongside, consumes this file).
- **Register #38** — `combined >= threshold` commits (boundary inclusive);
  FIELD_VERIFY carries candidate value in payload, redacted+blob-ref for PII paths.

## 5. Builder guardrails

- **No live LLM calls anywhere in this packet** — fakes only; the wrapper is the seam.
- All writes to `claim_fields` go through `claim_core`'s public write path — no
  direct table writes from `doc_intel` (append-only + PII + dictionary enforcement
  must not be bypassed). Reviewer greps for `ClaimField(` outside `claim_core`.
- Zero provenance ⇒ zero commit — no code path may commit an uncited field.
- Money: validators output integer cents; no float ever touches a Money value.
- Review-item emissions stay `review.created` events with the §1.2 type names —
  no new event types beyond `document.rejected`/`document.extracted` (already in the
  PRD-00 catalog).
- `.github/` untouched (CI change ships in the CTO packet commit: apt tesseract-ocr).

## 6. Definition of done (ED-7/ED-7a)

- All acceptance tests in `tests/acceptance/test_packet_04_docintel_substrate.py`
  pass unmodified; full suite green on SQLite and PostgreSQL legs.
- Unit ≥ 80% on `platform/doc_intel/`; validator table gets exhaustive
  boundary tests (every kenya_reg pattern, money_kes cents/no-cents/KSh variants,
  sum_check at exactly ±1_00).
- Alembic migration 0003 (`document_stages`).
- Runbook additions are CTO scope (docs/ ownership) — flag content to the reviewer
  in the PR description instead of writing docs/.
- ED-11: underdetermined → narrowest safe behaviour + register entry.
