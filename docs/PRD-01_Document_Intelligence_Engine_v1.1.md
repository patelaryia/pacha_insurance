## PRD-01 — Document Intelligence Engine (build spec)

> **v1.1** — incorporates the CTO decision round of 2026-07. Where this document conflicts with Section 0 (Shared Engineering Decisions) or Section 0.5 (Shared Agent Runtime), those files win. Anything underdetermined: follow ED-11 (never decide locally — route to the open-items register).

### 1.1 Purpose

Single shared pipeline: any inbound artifact → classified, parsed, extracted into `claim_fields` with citations and calibrated confidence. Agents never call LLMs on documents directly; they call this engine.

### 1.2 Pipeline (stages, each a Celery task, resumable per document)

```
document.received
 → NORMALIZE   msg/eml→PDF via rendering; images→PDF; XLSX kept native+CSV snapshot;
               render every PDF page to PNG @300dpi (S3: pages/{doc_id}/{n}.png —
               deleted at 180d, regenerated on demand, per ED-9);
               extract native text layer w/ word bboxes (PyMuPDF); OCR fallback
               (Tesseract 5) when text coverage <5% of page area; sha256 dedupe
 → CLASSIFY    MODEL_LIGHT, input: page-1 image + first 2k chars + filename + email
               subject; output: {doc_type ∈ pack taxonomy ∪ 'other', confidence}.
               <0.80 → review item DOC_CLASSIFY; 'other' → review item always
 → SPLIT?      heterogeneity detector (v1.0, mandatory): page-level MODEL_LIGHT
               classification runs when doc-level classify returns 'other'/low-conf
               OR page_count > 4 with mixed page classes → review item DOC_SPLIT:
               thumbnail-strip UI, officer draws boundaries; children created as
               documents with parent_document_id, each re-entering the pipeline
               from CLASSIFY. Human boundaries never misclassify. v1.1 (deferred):
               agent pre-fills proposed boundaries in the same UI (capability
               intake.doc_split, starts L1). The splitter contract, child-document
               schema, and DOC_SPLIT item are built NOW; only the proposal is later.
 → EXTRACT     MODEL_HEAVY per doc_type schema (below); structured tool-use output
 → CITE        anchor-match each field (1.4) → bbox provenance or citation_failed
 → VALIDATE    deterministic validators (1.5) → combined confidence
 → COMMIT      write claim_fields batch (source_type='extraction'); below-threshold
               fields → review item FIELD_VERIFY; emit document.extracted
 → CONSISTENCY cross-document rules (1.6) when trigger sets complete
```

Budgets: p95 ≤ 3 min/document end-to-end; cost ceiling US$0.60/document p95 (alert at breach — this is your gross-margin sentinel).

### 1.3 Extraction schema registry

Per `doc_type`, packs register a JSON Schema where every leaf carries: `type`, `required`, `validator` (named, see 1.5), `pii_class`, `confidence_threshold`, `target_path` (claim_fields path). The extraction prompt is **generated from the schema** (field name, description, format examples) — adding a medical invoice later = registering a schema, no prompt engineering project.

**Motor v1 document schemas** (field lists are the spec; engineers implement exactly these):

|doc_type|Fields (→ target path)|
|---|---|
|`intimation_email`|insured_name→`parties.insured.name`, reg→`vehicle.reg`, loss_date→`loss.date`, loss_time→`loss.time`, location→`loss.location`, narrative→`loss.narrative`, reporter_role (broker/agent/client)→`intimation.channel`, broker_name|
|`claim_form` (handwritten)|policy_no, insured contacts, driver_name, driver_dob, driver_licence_no, years_driving, loss description, third_party_present(bool), injuries(bool), diagram → `loss.diagram_description` (vision narrative, always flagged for human read)|
|`logbook`|reg, owner_name, chassis_no, engine_no, **bank_interest {present: bool, bank_name}**, logbook_no|
|`driving_licence`|name, dob, licence_no, first_issue_date, classes[], expiry|
|`kra_pin_cert`|pin, name|
|`police_abstract`|ob_number, station, report_date, parties[], remarks|
|`repair_estimate`|garage_name, line_items[{description, qty, unit_price, amount}], subtotal, vat, **total→`assessment.estimate_total`**|
|`assessor_report`|assessor_firm, **agreed_quote→`assessment.agreed_quote`**, **pav→`assessment.pav`**, repair_pct_pav, supplier_lines[{part, supplier, supplier_price, garage_price}], assessor_fee, salvage_value?, flags[], recommendation|
|`discharge_voucher`|amount, payee, signed(bool — vision check), date|
|`bank_discharge_letter`|bank, reg, discharge_confirmed(bool), payee_instructions|
|`photo_damage`|no field extraction; damage_description (vision), quality_ok(bool) — feeds consistency engine|

Validators referenced: `kenya_reg` v1.1 pattern set — standard `^K[A-Z]{2} ?\d{3}[A-Z]$`; pre-2000 legacy `^K[A-Z]{2} ?\d{3}$` (no suffix letter); motorcycles `^KM[A-Z]{2} ?\d{3}[A-Z]$`; trailers `^Z[A-Z] ?\d{4}$`; government `^GK ?[A-Z]? ?\d{3}[A-Z]?$`; diplomatic/military/NGO plates **out of scope v1** → classify-to-review, never auto-match. All patterns verified against the corpus in week 1 and corrected as pack config (they gate thread-matching, so they get corpus golden tests — open item 16), `kra_pin` (`^[AP]\d{9}[A-Z]$`), `date_past`, `money_kes` (**parses shilling-denominated strings from documents and multiplies by 100 on commit — storage is integer KES cents per ED-8**; explicit cents in source documents parsed when present; handles `KES/KSh/,` variants), `sum_check` (line items Σ = total, tolerance KES 1), `licence_no`, `phone_ke`.

### 1.4 Citation mechanics (mandatory, exact algorithm)

**Mode 1 — `anchor_text` (default):** the extraction schema forces the model to return, per field: `{value, anchor_text (verbatim ≤120 chars from source), page}`. Post-process: fuzzy-match `anchor_text` against that page's word-bbox stream (normalized Levenshtein ≥ 0.85 over a sliding window) → derive bbox → store in `source_ref`. Match failure ⇒ `citation_failed: true` ⇒ confidence forced to 0 ⇒ review item.

**Mode 2 — `vision_bbox` (v1.1):** the model returns normalized bbox coordinates directly. Permitted **only** for doc types flagged `handwritten: true` in the pack, or pages with < 5% text coverage. Verification reuses G-CITE's render-crop check (crop the claimed bbox, MODEL_LIGHT confirms the value is visible in the crop). Vision-cited fields carry a **×0.9 confidence multiplier** — stated consequence: most handwritten claim-form fields will land in `FIELD_VERIFY` at launch (~10 fields × ≤10s ≈ under 2 minutes per claim form — an accepted, sized review load; the corpus calibrates from there).

**A field cannot reach `verification_state='extracted'` without a resolved citation (either mode).**

**Swahili content (police abstracts):** structured fields (OB number, station, dates) are format-based and in scope for extraction. The remarks field is captured verbatim, plus a machine-translation gloss stored as a derived field flagged `machine_translated: true` — narrative-only, **never a rule input**. The console renders value ↔ highlighted region side-by-side (PRD-04); the grader re-checks citation-value consistency (PRD-03).

### 1.5 Confidence model

`combined = model_conf × V` where V: named validator pass = 1.0; fail = 0.30; not applicable = 0.95. Thresholds (defaults, overridable per field in schema): money/date/registration fields **0.90**, all else **0.85**. Below threshold → `FIELD_VERIFY` review item (field, value, citation viewer, accept/correct). Calibration job (PRD-03) recomputes reliability curves monthly from human corrections; thresholds are data, not code.

### 1.6 Cross-document consistency engine

Pack-defined checks, each `{id, trigger_docs[], expression, severity}` running when its trigger set is present. Motor v1: `CC-1` reg match logbook=claim=estimate=assessor_report (severity: block-pack); `CC-2` DL dob→computed age vs claim_form driver age ±1yr (flag); `CC-3` DL expiry ≥ loss date (block-pack); `CC-4` owner_name(logbook) ≈ insured_name (fuzzy ≥0.8, flag); `CC-5` **narrative-vs-photos**: MODEL_HEAVY vision compares `loss.narrative` against all `photo_damage` descriptions → `{consistent|inconsistent|insufficient, rationale, score}` — **always emits a review flag when not consistent, never blocks, never auto-clears** (D-01 boundary, hard-coded: this check type is not eligible for autonomy above L2). Results stored in `consistency_results`, surfaced on the claim and injected into the approval note's verification section.

### 1.7 Acceptance

Against the PRD-03 motor corpus: classification accuracy ≥ 97%; financial-field extraction ≥ 98%, all fields ≥ 95%; citation resolution ≥ 99% of committed fields; handwritten claim-form per-field accuracy baseline measured in week 1 and threshold set with Claims Manager (A-4/Q-07 discharge); zero fields committed without provenance. Failure-path tests: corrupt PDF, 40-page scan, XLSX estimate, photo-only intimation, duplicate attachment (sha dedupe).