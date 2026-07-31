# Intake-flow runbook

## Durable run states

One `intake.requested` event owns `pacha.intake.{trigger_event_ulid}` and one
`agent_runs` projection for the S1–S8 sequence. The control Worker registers
`IntakeWorkflow`; `intake_acknowledge` runs on the effects Worker with one
Temporal attempt. `awaiting_review` means the open review item is the recovery
surface; its committed `intake.review_resolved` control event carries only opaque
references and Signals the same Workflow. Do not edit `agent_runs.steps`, start a
second Workflow, or replay the original email event by hand.

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
3. Let the committed `document.stage_recovered` event Signal the existing
   `DocumentIntelligenceWorkflow`; never invoke an Activity directly or start a
   second execution.
4. When extraction commits, `intake.document_ready` Signals the existing intake
   Workflow and S3 reloads PostgreSQL idempotently. Confirm the projection advances
   from `populate`; do not manufacture either event or decrement an attempt count.

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
