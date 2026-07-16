# Intake-flow runbook

## Durable run states

One `intake.requested` event owns one `agent_runs` row for the S1–S8 sequence.
`awaiting_review` means the open review item is the recovery surface; resolving it
emits `review.resolved` and resumes the run. Do not edit `agent_runs.steps` or replay
the original email event by hand.

A creation confirm rejected before S1 commits is a terminal, successful no-op. The
run is `completed`, the creation-step outcome records `resolution: rejected` and
`result: no_op`, later steps record `claim_creation_rejected`, and no claim exists.
This is not a reaper incident.

## Waiting on document extraction

S3 records `waiting{expects_event: document.extracted}` while the synthetic
intimation-email document has not completed extraction. Waiting re-invocations do
not consume a run attempt, so the AR-1 reaper deliberately does not fail this run
after three polls. The source of truth is the document's `document_stages` rows:

1. Find the synthetic body document from the run's trigger message and inspect its
   first non-`succeeded`/`skipped` stage, including `status`, `attempts`,
   `last_error`, and `updated_at`.
2. If the stage is `paused`, `failed`, or left `running` by a worker crash, first
   establish that the prior worker cannot still commit. Then call
   `DocIntelEngine.recover_stage(document_id, stage, actor="system"|"user:<ULID>")`.
3. Enqueue the matching `doc_intel.<stage-lowercase>` task on the configured
   document-intelligence queue (or invoke `process_stage(..., schedule_next=True)`
   from the controlled worker recovery path). Never blind-retry an uncertain write.
4. When extraction commits, `document.extracted` resumes S3 idempotently. Confirm
   the run advances from `populate`; do not manufacture that event or decrement the
   stored attempt count.

The provider, budget, split, and stage-level recovery rules remain authoritative in
`docs/runbooks/doc_intel.md`.

## Triage review recovery

The Mode-A `coverage_manual` card is the only keying surface. Rejecting it creates a
new open card linked by `retry_of`; the claim stays `INTIMATED` and the run remains
`awaiting_review`. An R-02 row blocked because `assessment.estimate_total` is absent
is expected before PRD-06 chase: S8 still transitions the claim to `TRIAGED`, with
no exception. Re-evaluation occurs only through the owning later workflow when the
estimate arrives.

A below-excess decline release remains open with 409 while T-07 is
`pending_capture`. Once T-07 is captured and renderable, approval commits
`TRIAGED→DECLINED` and submits the letter through AR-3 under
`triage.decline_draft`. Until item-1 transport lands, resolving a subsequently
staged communication never fabricates `email.sent`.
