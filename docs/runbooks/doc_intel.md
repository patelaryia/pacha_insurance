# Document intelligence runbook

## Provider outage

Model calls use the shared bounded-retry wrapper: six attempts, exponential backoff
from one to sixty seconds, a ten-minute ceiling, and fallback after attempt three.
Transport exhaustion leaves the current durable stage failed for reaper-driven resume.
Schema-invalid output regenerates once and then emits `EXCEPTION` with subtype
`model_schema_invalid`; budget exhaustion emits `EXCEPTION{budget_exceeded}`.
Never manually replay a provider response or bypass `ModelWrapper`.

Before relying on provider fallback, replace each `pending_capture` fallback-model
slot in `packs/motor/doc_intel.yaml` with CTO-approved, supported pinned ids. Primary
models and standard list-price metering are configured. Keep tests on injected fakes.
Confirm the audit ledger records only redacted request/response detail.

## DOC_SPLIT backlog

Parents with mixed, low-confidence, or `other` page classifications pause before
EXTRACT and create one idempotent `DOC_SPLIT` review. Monitor the age and count of
these reviews. Officers must draw at least two contiguous, non-overlapping ranges
covering every page exactly once. Apply them through
`DocIntelEngine.apply_human_boundaries`; invalid ranges fail with
`422 INVALID_SPLIT_BOUNDARY`. Reapplying the same ranges returns the existing child
ids. Children are immutable PDF subsets linked by `parent_document_id`, with
NORMALIZE complete and CLASSIFY pending.

## Vision verification failure

Vision bboxes are eligible only for handwritten schemas or raster/text-sparse pages.
Coordinates must be finite, normalized, and have positive area. The engine crops the
immutable 300dpi render and asks MODEL_LIGHT whether the value is visible. Any
ineligible bbox, invalid crop, false response, or schema-invalid response forces
confidence to zero and routes `FIELD_VERIFY`; do not manually promote it to
`extracted`. Successful vision citations retain the 0.9 confidence multiplier.

## Duration and cost alerts

Every terminal attempt appends a `doc_intel_samples` row. Until PRD-03 defines a p95
window, each duration above 180,000 ms emits `DOC_INTEL_DURATION_BREACH` and each
cost above US$0.60 emits `DOC_INTEL_COST_BREACH`. Inspect the document's stage rows,
model-call ledger records, page count, OCR use, and retry history. Alerts are
operational signals, not review-item types, and must not be auto-cleared.
