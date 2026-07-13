# Document intelligence runbook

## Provider outage

Model calls use the shared bounded-retry wrapper: six attempts, exponential backoff
from one to sixty seconds, a ten-minute ceiling, and fallback after attempt three.
Transport exhaustion leaves the current durable stage paused for an explicit safe retry.
Schema-invalid output regenerates once and then emits `EXCEPTION` with subtype
`model_schema_invalid`; budget exhaustion emits `EXCEPTION{budget_exceeded}`.
Never manually replay a provider response or bypass `ModelWrapper`.

Workers send normalised text and rendered PNG/crop bytes through
`anthropic_client.py`; storage keys are never a substitute for provider content.
Audit records contain redacted text plus image byte counts/hashes, never image
base64. The adapter publishes `model.called` through the transactional event/outbox
spine, and only the concurrency-one ledger consumer materialises the ledger row.

Before relying on provider fallback, replace each `pending_capture` fallback-model
slot in `packs/motor/doc_intel.yaml` with CTO-approved, supported pinned ids. Until
then, reaching `pending_capture` raises the controlled provider-unavailable path and
pauses the durable stage; it never escapes as an unclassified adapter error. Primary
models and list-price metering are configured. Keep tests on injected fakes.

## Worker startup and stage recovery

Start workers with `DATABASE_URL`, `PACHA_BLOB_ROOT`, and
`DOC_INTEL_ALERT_SINK_FACTORY=module:factory`, plus
`PACHA_WORKER_RUNTIME_FACTORY=doc_intel.runtime:build_worker_runtime`. Each worker
constructs its own app services, Anthropic adapter, scheduler, and engine; it does not depend on an API
process global. Missing alert configuration fails startup. The Celery queue comes
from `packs/motor/doc_intel.yaml`.

`document.received` schedules NORMALIZE. Each successful or skipped task queues the
next named stage; failed or paused tasks queue nothing. Acquisition is an atomic
`pending -> running` transition. A crash deliberately leaves `running`; after
confirming the prior worker cannot still commit, an operator must call
`recover_stage(..., actor="system"|"user:<ULID>")`. No lease expiry or blind retry is
performed for uncertain work.

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
The predicate is exactly handwritten OR stored page-area coverage below 0.05; 0.05
is not eligible. Coverage is the summed native word-bbox area divided by page area.
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

Cost is summed from durable `model.called` events for the document, including every
billed schema-invalid response and calls made by a stage that later fails. Stage
output summaries are not the billing source of truth.

Operational startup requires a configured `AlertSink`; the synchronous driver uses a
loud logging sink, while the null sink is available only by explicit test injection.
Migration `0005_packet05_cto_hardening` adds the `running`
and `paused` stage states plus a unique expression index over
`(claim_id, check_id, evidence._input_fingerprint)` without changing Packet-05's
pinned `consistency_results` columns.
