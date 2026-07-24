# Temporal implementation master plan

**Status:** approved for implementation
**Owner approval:** 2026-07-24
**Go-live status:** not approved; staging evidence remains mandatory
**Applies to:** all durable orchestration, scheduled work and agent workflows
**Authority:** implements Section 0 ED-2 and Section 0.5 AR-1/AR-1a under
ADR-001. If this plan conflicts with Section 0 or Section 0.5, those documents
win and the coding agent must stop and register the conflict.

This document is the implementation specification for coding agents. It
contains decisions, not options. Agents must implement one work packet at a
time in the order below. They must not substitute a different package layout,
queue topology, encryption design, migration order or retry policy without an
owner-approved amendment to this file.

## 1. Executive instruction

Pacha is not live. Build Temporal-first and remove Celery/Redis orchestration
before launch. Do not build a runtime selector, permanent compatibility layer,
dual-write path or production shadow engine.

Retain:

- PostgreSQL claims, append-only fields, domain events and event-delivery rows;
- the transactional outbox pattern;
- the single-writer audit ledger;
- `agent_runs` as Pacha's readable audit/operations projection;
- COP step definitions, business rules, packs and calculations;
- `execute_or_stage`, autonomy ceilings and human authority;
- external-write idempotency, readback and reconciliation;
- synchronous domain services that Activities call.

Replace:

- `AgentRunner` workflow position and recovery;
- the stale-run reaper;
- Celery task wrappers;
- Celery Beat schedules;
- Redis as an orchestration dependency;
- in-process assumptions that a human wait or timer is an agent-runner state
  transition.

Temporal owns orchestration only. It is never the claim database, business
event ledger, approval authority or record of an external write.

## 2. Delivery sequence and PR boundaries

Implement exactly these packets. Each packet is one reviewable PR and must be
green before the next begins.

| Packet | Scope | Must not include |
|---|---|---|
| T00 | Approval/specification alignment | Runtime code |
| T01 | Shared Temporal package, configuration, Codec, Worker bootstrap and SDK tests | Agent migration, Celery deletion |
| T02 | `agent_runs` Temporal projection, event/outbox bridge, system drain Workflows and review Signal routing | Business-agent migration |
| T03 | Complete PRD-06 document-chase Workflow | Other agents |
| T04 | PRD-01 document-intelligence Workflow and long-running Activities | Intake, assessment or approval |
| T05 | Intake and assessment Workflows | Approval/projection |
| T06 | Approval-pack and projection Workflows | Celery deletion |
| T07 | Temporal Schedules for all recurring platform jobs | Infrastructure |
| T08 | Remove Celery, Beat, reaper, task wrappers and Redis dependency | Cloud deployment |
| T09 | ECS/Fargate Worker deployment, IAM, secrets, KMS and observability | Business behaviour changes |
| T10 | Cloud/RDS failure trial, runbooks and go-live evidence | New product scope |

No packet may hide unfinished work in a future packet when that work is part of
its acceptance list.

## 3. Target runtime topology

```text
FastAPI request
  → PostgreSQL transaction
      → claim/field/review mutation
      → append domain event
      → event_deliveries/outbox intent
  → response (does not wait for Temporal)

Temporal Schedule: outbox-drain (every 30 seconds)
  → OutboxDrainWorkflow
      → dispatch_nonledger_events Activity
          → existing idempotent Postgres consumers
          → start/signal Workflow with stable ID
          → mark event delivery succeeded

Business Workflow
  → control/read Activity
      → load authoritative Postgres state
      → commit domain event/read-model projection
  → durable timer or opaque Signal wait
  → governed-effect Activity
      → execute_or_stage
      → independent readback/reconciliation
  → projection Activity
      → update agent_runs

Temporal Schedule: ledger-drain (every 10 seconds)
  → LedgerDrainWorkflow
      → append_ledger_batch Activity on ledger Task Queue
          → PostgreSQL advisory transaction lock
          → append events strictly by seq
```

Claim create/read/update APIs must remain available when Temporal is
unavailable. Temporal failure may delay asynchronous progress but must never
roll back a committed Pacha transaction or make claim reads depend on a
Temporal Query.

## 4. Package and file layout

Build this shared package incrementally. T01 creates the core files listed in
section 26; T02 adds `activities.py`, `starter.py` and `workflows.py`; T07 adds
`schedules.py`. By T07 the package is:

```text
platform/orchestration/
  __init__.py
  activities.py
  client.py
  codec.py
  config.py
  contracts.py
  errors.py
  history.py
  ids.py
  policies.py
  schedules.py
  starter.py
  worker.py
  workflows.py
```

T01 public imports exposed by `orchestration.__init__`:

```python
TemporalConfig
build_temporal_client
build_data_converter
WorkflowRef
ControlResult
build_worker
```

T02 adds `TemporalStarter`; T07 adds `bootstrap_schedules`. Do not export a
placeholder before its packet.

Other packages may import only those public names. They must not import
`temporalio` directly except inside their own `workflows.py` and
`activities.py`. Cross-package domain calls continue through existing public
interfaces.

Each migrated package adds, at most:

```text
<package>/activities.py
<package>/workflows.py
```

Do not create a microservice, generic plugin framework, workflow DSL or
reflection-based Activity registry.

## 5. Dependency decisions

T01 changes the root runtime dependencies:

```text
temporalio==1.30.0
boto3>=1.35,<2
```

Keep the existing `cryptography` dependency. T08 removes:

```text
celery
redis
```

The isolated spike keeps its own requirements and remains as historical
evidence. Production code must not import from `spikes/`.

## 6. Temporal connection configuration

Use this exact environment contract:

| Variable | Required | Rule |
|---|---:|---|
| `PACHA_ENV` | yes | `dev`, `test`, `staging` or `prod` |
| `PACHA_TEMPORAL_MODE` | yes | `local` only for `dev/test`; `cloud` for `staging/prod` |
| `PACHA_TEMPORAL_ADDRESS` | cloud | Namespace endpoint with port |
| `PACHA_TEMPORAL_NAMESPACE` | yes | Explicit; no implicit `default` outside tests |
| `PACHA_TEMPORAL_REGION` | cloud | Telemetry/DPIA label; must match approved namespace |
| `PACHA_TEMPORAL_TLS_CERT_SECRET_ARN` | cloud | Secrets Manager ARN containing PEM client certificate |
| `PACHA_TEMPORAL_TLS_KEY_SECRET_ARN` | cloud | Secrets Manager ARN containing PEM private key |
| `PACHA_TEMPORAL_KMS_KEY_ARN` | cloud | Immutable customer-managed symmetric KMS key ARN in `...:key/{uuid}` form; alias ARNs are refused |
| `PACHA_TEMPORAL_QUEUE_PREFIX` | yes | Exactly `pacha-{env}` |
| `PACHA_BUILD_ID` | yes | Immutable git commit SHA deployed in the image |
| `PACHA_WORKER_ROLE` | Worker | `control`, `docintel`, `effects` or `ledger` |

Rules:

- `staging/prod` must refuse to start in `local` mode.
- Cloud mode uses mTLS through `temporalio.client.TLSConfig`.
- Certificate/key bytes are fetched once at process start with
  `secretsmanager:GetSecretValue`, retained only in process memory and never
  logged or written to disk.
- Do not add API-key fallback in v1.
- All clients use the same encrypted Data Converter and set
  `HeaderCodecBehavior.CODEC`.
- A configuration error terminates the Worker before polling.
- `build_temporal_client` accepts injected `SecretBytesProvider` and
  `DataKeyProvider` implementations only in `dev/test`. `staging/prod` refuse
  every injected provider regardless of its concrete type and construct the
  AWS Secrets Manager and KMS implementations themselves.

## 7. Task Queues and Worker services

Queue names are constructed as
`{PACHA_TEMPORAL_QUEUE_PREFIX}-{role}-v1`.

| Role | Queue | Work | Production service count | Worker Activity concurrency |
|---|---|---|---:|---:|
| control | `pacha-{env}-control-v1` | Workflows, DB/control Activities, schedules | 2 | 20 |
| docintel | `pacha-{env}-docintel-v1` | OCR, vision, LLM and document stages | 2 | 4 |
| effects | `pacha-{env}-effects-v1` | `execute_or_stage` governed effects/readback | 1 initially | 5 |
| ledger | `pacha-{env}-ledger-v1` | ledger append and verification only | 1 | 1 |

All roles use the same application image with `PACHA_WORKER_ROLE` selecting the
registrations. No Worker has an inbound port.

Ledger single-writer safety does not rely solely on ECS desired count. Every
ledger-append Activity must acquire:

```sql
SELECT pg_advisory_xact_lock(hashtext('pacha:audit-ledger-writer'));
```

before reading the next unledgered sequence or appending. SQLite tests use the
existing process lock analogue.

Set Worker graceful shutdown to 60 seconds and ECS stop timeout to 120 seconds.
Activity code must honour cancellation at safe boundaries.

## 8. Worker Deployment and Workflow compatibility

Every Worker uses:

```python
WorkerDeploymentConfig(
    version=WorkerDeploymentVersion(
        deployment_name=f"pacha-{env}-{role}",
        build_id=PACHA_BUILD_ID,
    ),
    use_worker_versioning=True,
    default_versioning_behavior=VersioningBehavior.PINNED,
)
```

Rules:

- Business Workflows declare
  `versioning_behavior=VersioningBehavior.PINNED`.
- Finite executions remain on their starting build.
- System drain Workflows are short-lived and also pinned; schedules start new
  executions on the current build.
- No infinite Workflow is permitted.
- A code change that alters recorded Workflow commands requires
  `workflow.patched("descriptive-version-id")` or a new Workflow type.
- Every Workflow change replays all committed history fixtures before merge.
- Retain an old Worker deployment until Temporal reports no pinned open
  executions for that build.
- Never delete a patch marker while any retained history can replay through it.
- The Workflow sandbox may pass through only these exact deterministic modules:
  `orchestration.contracts`, `orchestration.errors`, `orchestration.ids` and
  `orchestration.policies`. It must not pass through the `orchestration`
  package root, `client`, `codec`, `config`, `worker` or any module with I/O,
  credentials, randomness or mutable runtime state. Package exports must be
  lazy where necessary so importing an approved module cannot transitively
  import a forbidden one.

## 9. Workflow identity and duplicate policy

Workflow IDs contain no customer data:

| Workflow | ID |
|---|---|
| generic agent run | `pacha.agent.{agent_run_ulid}` |
| document chase | `pacha.chase.{checklist_ulid}` |
| document intelligence | `pacha.docintel.{document_ulid}` |
| intake | `pacha.intake.{trigger_event_ulid}` |
| assessment | `pacha.assessment.{agent_run_ulid}` |
| approval pack | `pacha.approval-pack.{agent_run_ulid}` |
| projection | `pacha.projection.{projection_ulid}` |
| outbox drain execution | Temporal Schedule-generated ID |
| ledger drain execution | Temporal Schedule-generated ID |

Start policy:

```text
WorkflowIDReusePolicy.REJECT_DUPLICATE
WorkflowIDConflictPolicy.USE_EXISTING
```

The Pacha business ULID is generated and committed before the start attempt.
Retries must reuse the same Workflow ID. Never derive an ID from timestamps,
names, registration plates, policy numbers or hashes of PII.

The outbound client interceptor enforces both policies above; a call site
cannot omit or override them.

## 10. Control-only payload contract

Temporal payloads may contain only:

```text
run_ref
claim_ref
workflow_ref
workflow_run_ref
trigger_event_ref
event_ref
event_seq
review_event_ref
document_ref
checklist_ref
projection_ref
schedule_ref
pack_version
payload_hash
write_id
step_id
status
wake_at_epoch_ms
timer_seconds
attempt_no
```

All are strings or integers. Strings are limited to 160 UTF-8 bytes. The
complete unencoded argument or result collection is limited to 8 KiB, not each
argument independently. Validation uses one running canonical-JSON byte budget,
fails on the first byte over the limit and applies a finite nesting cap.

Value validation is closed:

- `run_ref`, `claim_ref`, `trigger_event_ref`, `event_ref`,
  `review_event_ref`, `document_ref`, `checklist_ref` and `projection_ref` are
  exactly one uppercase 26-character ULID;
- `workflow_ref` must match one Workflow-ID form in section 9;
- `workflow_run_ref` is a canonical UUID string supplied by Temporal;
- `payload_hash` is 64 lowercase hexadecimal characters;
- `write_id` matches `^[a-z0-9][a-z0-9:._-]{0,159}$` and is constructed only
  from fixed operation names plus opaque ULIDs/integers;
- `step_id`, `status` and `pack_version` come from closed registries;
- integer fields are non-negative and `wake_at_epoch_ms` is UTC epoch time.

No contract permits arbitrary strings or dictionaries.

Forbidden in Workflow inputs, results, Signals, Queries, heartbeat details,
memo, search attributes and exception messages:

- names, email addresses, phone numbers or physical addresses;
- policy or registration numbers;
- national IDs, KRA PINs, driving-licence data or bank data;
- claim documents, extracted fields, narratives or generated prose;
- money amounts, reserves, estimates, payable values or settlement values;
- recipient lists, target payloads, LLM inputs/outputs or citations;
- credentials, URLs containing secrets or raw exception text.

T01 installs a mandatory `ControlPayloadConverter` ahead of serialization in
the encrypted Data Converter. This converter is the completeness boundary: it
validates Workflow, Activity, Signal and Query inputs and results, heartbeat
details, headers and failure details, including the complete-collection 8 KiB
limit. The outbound client interceptor has the separate job of enforcing
Workflow IDs and duplicate policy and refusing unapproved SDK surfaces.
Validation failure raises before serialization or the SDK call. Tests inspect
fetched history for seeded sentinel values.

Do not use Memo, custom Search Attributes, custom headers, static summary/detail
text, Workflow Updates, Nexus operations or cron starts in v1. Recurring work
uses Temporal Schedules. Pacha operations screens read `agent_runs`; they do
not query Temporal visibility for claim facts.

## 11. Payload Codec

Production uses an AES-256-GCM Payload Codec with AWS KMS envelope encryption:

1. For each `encode(payloads)` batch, call KMS `GenerateDataKey` once with
   `KeySpec=AES_256`.
2. Encrypt each complete serialized Temporal `Payload` with the returned
   plaintext data key and a unique 12-byte nonce.
3. Use AAD:
   `pacha-temporal-codec-v1|{namespace}|{kms_key_arn}`.
4. Store only this metadata with each encoded payload:
   `encoding=binary/pacha-aesgcm-v1`, KMS key ARN, wrapped data-key bytes,
   nonce and Codec version.
5. Zero the in-process plaintext-key byte buffer after the batch as far as
   Python permits and discard the reference.
6. Decode by grouping payloads with the same wrapped data key, calling
   `Decrypt` once per group, authenticating AAD and parsing the original
   Payload.
7. Refuse unknown Codec versions, key ARNs outside the configured allowlist,
   malformed nonces, KMS failure or authentication failure. Never fall back to
   plaintext.

KMS calls use a boto3 client configured with 2-second connect timeout, 5-second
read timeout and standard mode with maximum three attempts. Because the Codec
API is asynchronous and boto3 is synchronous, invoke KMS through
`asyncio.to_thread` behind a process-local semaphore of eight. Do not cache a
plaintext data key across encode calls.

V1 uses one immutable customer-managed symmetric KMS key ARN and refuses alias
ARNs. Rotate key material using AWS KMS automatic or on-demand key-material
rotation under that same ARN; KMS retains prior material so old wrapped data
keys remain decryptable. Replacing the KMS key with a different ARN is a
separate reviewed migration and is not alias retargeting. The Worker IAM role
receives `kms:GenerateDataKey` and `kms:Decrypt` only for the approved key ARN.

Tests use a static synthetic 32-byte provider. Every injected secret or data-key
provider is refused when `PACHA_ENV` is `staging` or `prod`.

Encryption is defence in depth. The control-only rule still applies before
encryption.

## 12. Error and retry constitution

Retry policies live in `packs/motor/orchestration.yaml`, but the following
ceilings are hard-coded and pack data may only tighten them:

| Policy | Initial | Backoff | Maximum | Attempts | Timeout |
|---|---:|---:|---:|---:|---:|
| `db_control` | 1s | 2.0 | 30s | 5 | start-to-close 60s |
| `long_compute` | 2s | 2.0 | 30s | 3 | start-to-close 2h, heartbeat 30s |
| `provider_managed_retry` | n/a | n/a | n/a | 1 Temporal attempt | start-to-close from existing provider policy |
| `governed_external_write` | n/a | n/a | n/a | 1 | start-to-close 2m |
| `ledger_append` | 1s | 2.0 | 10s | 5 | start-to-close 60s |

`provider_managed_retry` means the Activity calls the existing ED-4a/provider
wrapper, which owns its bounded retries. Temporal must not multiply them.

Every Activity catches internal exceptions, records redacted diagnostic detail
to Pacha/Sentry and raises a sanitised `ApplicationError`. Allowed Temporal
failure types:

```text
blocked_on_inputs
domain_rejected
human_review_required
uncertain_write
ui_drift
payload_diverged
idempotency_conflict
provider_exhausted
activity_internal
```

Raw exception strings must never reach Temporal history.

These types are non-retryable:

```text
blocked_on_inputs
domain_rejected
human_review_required
uncertain_write
ui_drift
payload_diverged
idempotency_conflict
```

A governed external-write Activity always has `maximum_attempts=1`. Any timeout,
Worker loss or connection loss after it is scheduled is treated as uncertain
unless an Activity using the target-specific read/probe proves non-execution.
Only a new, explicit Pacha event may authorise another attempt using the same
stable write ID.

## 13. `agent_runs` schema and ownership

T02 creates Alembic revision `0016_temporal_runtime.py` and changes the binding
schema to:

```sql
CREATE TABLE agent_runs (
  id TEXT PRIMARY KEY,
  agent TEXT NOT NULL,
  capability_id TEXT NOT NULL,
  claim_id TEXT,
  trigger_event TEXT REFERENCES events(id),
  workflow_id TEXT NOT NULL UNIQUE,
  workflow_run_id TEXT,
  workflow_type TEXT NOT NULL,
  worker_build_id TEXT,
  status TEXT NOT NULL CHECK (
    status IN (
      'pending', 'running', 'awaiting_review', 'blocked',
      'completed', 'failed', 'cancelled'
    )
  ),
  steps JSONB NOT NULL DEFAULT '[]',
  autonomy_level TEXT NOT NULL,
  error JSONB,
  last_workflow_event_ref TEXT,
  last_synced_at TIMESTAMPTZ,
  started_at TIMESTAMPTZ NOT NULL,
  ended_at TIMESTAMPTZ
);
CREATE INDEX ix_agent_runs_status ON agent_runs(status);
CREATE INDEX ix_agent_runs_claim ON agent_runs(claim_id);
```

Migration behaviour for existing development rows:

- `workflow_id = 'pacha.legacy.agent.' || id`
- `workflow_type = 'LegacyAgentRun'`
- `worker_build_id = 'legacy-celery'`
- existing statuses remain valid

There is no live data, but the migration must still upgrade and downgrade
cleanly in SQLite and PostgreSQL tests.

Ownership:

- The initiating Pacha transaction inserts `status='pending'`.
- An Activity records the Temporal `workflow_run_id` and changes to `running`.
- Activities append/update `steps` after domain commits.
- Human-review creation sets `awaiting_review`.
- Terminal Workflows set `completed`, `blocked`, `failed` or `cancelled`.
- The console and APIs read this table only.
- A reconciliation Activity can rebuild a row from Pacha events and Temporal
  control events; it never writes claim truth from a Temporal Query.

## 14. Transactional outbox and Temporal bridge

Keep `events` and `event_deliveries`. Do not add an
`orchestration_commands` table.

T02 adds `TemporalIntentConsumer` to the existing dispatcher. It handles only
registered event-to-Workflow mappings. Unknown event types are ignored, not
guessed.

Each mapping declares:

```text
event_type
workflow_type
workflow_id_builder
action: start | signal
signal_name when action=signal
control_contract_type
```

Mappings are code-owned because they define system behaviour. Business cadence,
thresholds and templates remain pack data.

Delivery algorithm:

1. Claim/review transaction commits its domain event.
2. `event_deliveries` exposes the event to `temporal_intent`.
3. `OutboxDrainWorkflow` invokes `dispatch_nonledger_events`.
4. The Activity claims one event-delivery row using existing Postgres locking.
5. It starts or Signals Temporal using the stable Workflow ID and opaque event
   reference.
6. Only after the SDK acknowledges does it mark the delivery succeeded.
7. If step 6 fails, the next drain repeats step 5. Start uses `USE_EXISTING`;
   Signals are de-duplicated by event ID in the Workflow and by idempotent
   database application.
8. Existing dead-letter behaviour emits `ops.alert`; it never discards the
   domain event.

The bridge must not run inside the FastAPI request. API success depends only on
the PostgreSQL commit.

## 15. Signals and human review

Signals carry exactly one opaque `event_ref`.

Standard Signal names:

```text
pacha_event
review_resolved
claim_terminal
document_received
snooze_changed
inbound_received
```

Signal handlers only enqueue the reference in deterministic Workflow state and
wake the main loop. They do not call databases or make business decisions.

The main Workflow calls an Activity with the event reference. That Activity:

- loads the committed event;
- validates type, claim/run association and actor authority;
- applies it idempotently;
- returns a control-only disposition.

Each Workflow keeps a bounded set of processed event references. Before
Continue-As-New, it persists the high-water event sequence in PostgreSQL and
passes only that opaque sequence integer to the new run. Duplicate resolution
events must not duplicate a send, transition or approval.

## 16. System Workflows and Schedules

T02 implements:

```text
OutboxDrainWorkflow
LedgerDrainWorkflow
SlaEvaluationWorkflow
LedgerVerificationWorkflow
```

All are finite: drain Workflows process at most ten 50-row batches and finish.
The next Schedule execution handles remaining backlog.

T07 adds these finite wrappers around the existing idempotent domain services:

```text
NotifyDigestWorkflow
GraphDeltaWorkflow
GraphRenewalWorkflow
WeeklyEvaluationWorkflow
PasteReadbackSampleWorkflow
```

The wrappers contain no business logic; each invokes one Activity and ends.
Graph delta tokens/subscription expiry remain in the Pacha integration store,
not Workflow history.

T07 creates these stable Schedule IDs:

| ID | Timing | Overlap | Catch-up |
|---|---|---|---|
| `pacha-{env}-outbox-drain-v1` | every 30s | SKIP | 5m |
| `pacha-{env}-ledger-drain-v1` | every 10s | SKIP | 5m |
| `pacha-{env}-sla-evaluate-v1` | every 5m | SKIP | 30m |
| `pacha-{env}-ledger-verify-v1` | 01:00 UTC daily | BUFFER_ONE | 24h |
| `pacha-{env}-notify-digest-v1` | 05:00 UTC daily | BUFFER_ONE | 24h |
| `pacha-{env}-graph-delta-v1` | every 60s | SKIP | 5m |
| `pacha-{env}-graph-renew-v1` | every 71h | BUFFER_ONE | 24h |
| `pacha-{env}-eval-weekly-v1` | existing pack-configured weekly time | BUFFER_ONE | 7d |
| `pacha-{env}-paste-readback-v1` | Monday 05:00 UTC | BUFFER_ONE | 24h |

Use `ScheduleOverlapPolicy.SKIP` and `BUFFER_ONE` exactly as listed.
`pause_on_failure=False`; failures page operations and the next occurrence
retries idempotently. Bootstrap creates missing schedules and compares existing
definitions. A mismatch fails deployment; bootstrap never silently overwrites
an existing production schedule.

## 17. First vertical: PRD-06 document chase

T03 implements `DocumentChaseWorkflow`, one execution per checklist:

```text
1. record_start
2. load_chase_state
3. if terminal/suppressed → record_cancelled → return
4. if initial request required → governed_chase_send
5. load_next_wake
6. wait for durable timer or Signal
7. apply_wake_event
8. reload authoritative state
9. inbound within window → persist defer → go to 5
10. snoozed → go to 5
11. due and below cap → governed_chase_send → go to 5
12. at cap → create chase_exhausted review → await review Signal
13. resolution says continue → reload schedule → go to 5
14. resolution says close or claim terminal → record terminal result → return
```

Activity boundaries:

- `record_chase_started(run_ref, checklist_ref, trigger_event_ref)`
- `load_chase_state(run_ref, checklist_ref)`
- `apply_chase_event(run_ref, checklist_ref, event_ref)`
- `governed_chase_send(run_ref, checklist_ref, write_id)`
- `create_chase_exception(run_ref, checklist_ref, event_ref)`
- `record_chase_terminal(run_ref, checklist_ref, status)`

`load_chase_state` returns only:

```text
status
step_id
wake_at_epoch_ms
payload_hash
event_ref
attempt_no
```

It does not return recipient, document, claim or money data.

Before every send, `governed_chase_send` reloads:

- claim state and suppression status;
- current outstanding checklist items;
- snooze and inbound deferral state;
- requester existence;
- reminder count and cap;
- current pack/template version.

It then calls the existing communications path through `execute_or_stage`.
The write ID is:

```text
chase:{checklist_id}:{reminder_index}
```

The write ID contains no PII and is persisted before execution. A stale timer
therefore cannot send after document receipt, terminal claim state or snooze.

T03 deletes the clock-driven `ChaseAgent.tick` production path after equivalent
Temporal acceptance tests pass. Pure selection/calculation helpers may remain
and be called by Activities.

## 18. Remaining business Workflow decisions

### T04 — document intelligence

- One `DocumentIntelligenceWorkflow` per document ULID.
- Existing stage order remains the PRD-01 order.
- Each stage is an Activity; no generic Celery wrapper remains.
- OCR/LLM/vision Activities use the docintel queue and heartbeat every 30
  seconds with stage/checkpoint integers only.
- The Workflow passes document ID and stage ID only.
- Activities load S3/object/database data internally.
- Existing stage database rows remain restart/idempotency truth.
- Provider retries remain inside the existing provider wrapper; Temporal
  attempt count is one.
- A failed or paused stage is resumed only from a committed Pacha event or
  authorised Signal.

### T05 — intake and assessment

- `IntakeWorkflow` ID uses the trigger event ULID so duplicate mail/webhook
  delivery attaches to the same execution.
- Claim creation remains a PostgreSQL Activity transaction.
- Once claim ULID exists, record it in `agent_runs`; do not change Workflow ID.
- Assessment uses one Workflow per `agent_runs.id`.
- Vendor selection, report parsing and cascade steps remain Activities.
- Human MODE_CONFIRM and related waits use `review_resolved`.
- Dispatch is a governed effect and has one Temporal attempt.

### T06 — approval pack and projection

- Approval-pack generation is one Workflow per agent run.
- Rendering Activities refuse missing/under-verified input exactly as today.
- Generated PDF bytes remain in S3, never Workflow history.
- PACK_REVIEW and signing waits use opaque review events.
- Projection paste-assist remains supported without an external effect.
- Vendor executor operations, when later approved, run only on the effects
  queue through `execute_or_stage`.
- Readback/reconciliation is a separate Activity after apparent execution.
- `uncertain_write` and `diverged` end the run blocked; neither is auto-fixed.

## 19. Celery/Redis removal inventory

T08 removes or refactors every item below after its Temporal replacement is
green:

- delete `platform/claim_core/celery_app.py`;
- remove `celery_app` from `claim_core.__init__`;
- remove `configure_runtime` calls from the FastAPI application;
- delete stale-run/reaper code from `platform/agent_runtime/runner.py`;
- replace the remaining runner surface with Temporal starter/projection
  services; delete the file if no pure helper remains;
- delete `platform/doc_intel/tasks.py`;
- replace `CeleryStageScheduler` in `platform/doc_intel/runtime.py`;
- delete `platform/eval_harness/tasks.py`;
- delete `agents/projection_agent/tasks.py`;
- remove Celery Beat registration from `platform/notify/digest.py`;
- remove pack/config keys used only for Celery queue routing;
- remove Celery and Redis from `requirements.txt`;
- remove Redis environment variables, runbook text and infrastructure;
- replace tests that assert Celery task names or Beat entries with Temporal
  Workflow/Schedule assertions.

Final repository checks:

```bash
rg -n "from celery|import celery|celery_app|CeleryStageScheduler|configure_reaper|reap_stale_runs" .
rg -n "redis://" .
```

Both must return no production-code matches. Historical architecture documents
and the isolated spike may mention Celery as superseded history.

## 20. Infrastructure implementation

T09 uses Terraform and the existing modular-monolith image.

Create:

```text
infra/terraform/modules/temporal_worker/
infra/terraform/environments/staging/
infra/terraform/environments/prod/
```

The module provisions:

- one ECS task definition per Worker role using the same image;
- ECS services with counts from section 7;
- no load balancer and no inbound security-group rules;
- scoped egress to Temporal Cloud and required AWS/provider endpoints;
- CloudWatch log groups and alarms;
- task roles with least-privilege RDS/S3/KMS/Secrets permissions;
- Secrets Manager ARNs and IAM permission to fetch the mTLS material in memory;
- deployment labels containing git build ID and Worker role.

Deployment order:

1. database migration;
2. one-shot `python -m orchestration.bootstrap` ECS task;
3. ledger Worker;
4. control Workers;
5. docintel Worker;
6. effects Worker;
7. API deployment;
8. smoke test and schedule-definition verification.

Rollback deploys the previous immutable image/build for the affected Worker
Deployment. Never roll back the database without a reviewed migration plan.

## 21. Observability

Every Workflow/Activity log line includes only:

```text
workflow_id
workflow_run_id
workflow_type
activity_type
run_ref
step_id
attempt
task_queue
build_id
status/error_code
duration_ms
```

No claim facts are logged.

Required metrics:

- Workflow starts/completions/failures/cancellations by type;
- Activity latency/retries/failures by type;
- Task Queue schedule-to-start latency;
- outbox and ledger backlog age/count;
- open `agent_runs` by status;
- Signal-to-resume latency;
- uncertain-write and divergence counts;
- Payload Codec/KMS failure count;
- schedule failures and missed/caught-up executions;
- pinned open Workflows by build ID.

Required alerts:

- oldest outbox delivery > 5 minutes;
- oldest unledgered event > 60 seconds;
- any ledger hash-chain verification failure;
- effects Activity `uncertain_write`;
- control Task Queue schedule-to-start p95 > 30 seconds for 10 minutes;
- no control Worker polling for 2 minutes;
- Payload Codec/KMS failure;
- Schedule action failure;
- Workflow failure rate > 1% over 15 minutes, excluding declared blocked states.

## 22. Test requirements

Every packet runs the repository checks in `AGENTS.md`.

Temporal tests use `WorkflowEnvironment.start_time_skipping`; do not mock the
Workflow engine for acceptance behaviour.

Required Temporal integration tests are mandatory and must fail when the test
server cannot start. They may not catch startup errors and convert them to
`skip`, `skipif`, `importorskip` or an equivalent green result. An explicitly
separate developer-only test command may omit the integration directory, but
the repository and CI checks used for packet acceptance must execute it.

Required suites:

1. **Unit:** config validation, ID builders, control-payload validator, retry
   policy ceilings, Codec round trip/tamper/unknown-key failure.
2. **Workflow:** happy path, duplicate start, Signal wait, timer, cancellation,
   Continue-As-New where used and deterministic Query output.
3. **Failure injection:** Worker shutdown mid-Activity, heartbeat recovery,
   different Worker continuation, Temporal connection loss, database transient
   failure and effects timeout.
4. **Replay:** every committed history fixture replays under the new build.
5. **Privacy:** seeded name, policy, registration, bank, money and document
   sentinels do not occur in fetched history or exception messages.
6. **PostgreSQL integration:** outbox locking, delivery retry, `agent_runs`
   projection/rebuild, ledger advisory lock and strict sequence.
7. **Acceptance:** one complete document chase with initial request, 30-day
   time skip, inbound deferral, document Signal, human review and terminal
   suppression.
8. **Availability:** stop Temporal/Workers and prove claim list/detail reads
   still succeed from PostgreSQL.

Protected acceptance tests may change only to replace a superseded Celery
assertion with an equal or stronger Temporal assertion. They must not be
deleted, skipped or weakened. Owner review remains required.

Coverage remains:

- at least 80% for `platform/*` and `agents/*`;
- 100% for pack calculations;
- every new Workflow branch exercised;
- every Activity error classification exercised.

## 23. Security and safety review checklist

Every Temporal PR reviewer must verify:

- no PII or money in Workflow/Signal/heartbeat/failure contracts;
- the validating Payload Converter is installed on every Data Converter and
  enforces the complete-collection 8 KiB limit before serialization;
- Codec enabled on all clients and headers;
- no memo, search attributes, custom headers, static summary/details, Workflow
  Updates, Nexus operations or cron starts;
- all database access occurs in Activities;
- Workflow code is deterministic and sandboxed, with only the four exact
  deterministic orchestration modules passed through;
- no direct external write outside `execute_or_stage`;
- external-write retry maximum is one;
- stable Workflow and write IDs;
- human resolution loaded from PostgreSQL, not trusted from Signal payload;
- `agent_runs` is a projection, not claim truth;
- claims remain readable without Temporal;
- no payment-execution operation exists;
- no Celery compatibility layer was introduced;
- pack/config values were not hard-coded into Workflow logic.

## 24. Coding-agent operating rules

For every packet, the coding agent must:

1. Read `AGENTS.md`, the source-of-truth documents and this entire file.
2. Inspect git status and preserve unrelated/user changes.
3. Work only on the named packet.
4. Update the packet's spec/DDL first if implementation exposes a genuine
   conflict; under ED-11, stop and register unresolved input rather than guess.
5. Use public package interfaces.
6. Add migration, tests, OpenAPI/runbook changes when applicable.
7. Run all applicable checks and report exact results.
8. List every changed file and any remaining blocker.

The coding agent must not:

- redesign decisions in this plan;
- self-host Temporal;
- add Step Functions, Celery or another workflow abstraction;
- build a general-purpose workflow DSL;
- put claim data into Temporal because it is encrypted;
- treat Temporal completion as external-write truth;
- retry an uncertain write;
- edit protected tests merely to get green CI;
- implement RPA, auction-provider or payment scope inside a Temporal packet;
- proceed into the next packet.

## 25. Definition of implementation complete

Temporal implementation is complete when:

- T01–T09 are merged and green;
- every durable agent and recurring job uses Temporal;
- the FastAPI service has no runtime dependency on Temporal for claim reads;
- Celery/Redis production code and dependencies are gone;
- all Workflows pass replay and privacy tests;
- `agent_runs`, outbox and ledger rebuild/reconciliation tests pass;
- ECS Workers and schedules are observable in staging;
- the full repository and frontend checks are green;
- runbooks cover outage, rollback, stuck Workflow, uncertain write and Codec
  failure.

Go-live remains a separate decision. T10 must demonstrate the failure matrix in
the approved Cloud/RDS environment and attach the evidence to
`docs/architecture/temporal_reliability_report.md`. No coding agent may mark
Pacha production-ready merely because implementation is complete.

## 26. T01 exact acceptance contract

The first coding agent implements T01 only.

Required files:

```text
requirements.txt
platform/orchestration/__init__.py
platform/orchestration/client.py
platform/orchestration/codec.py
platform/orchestration/config.py
platform/orchestration/contracts.py
platform/orchestration/errors.py
platform/orchestration/history.py
platform/orchestration/ids.py
platform/orchestration/policies.py
platform/orchestration/worker.py
packs/motor/orchestration.yaml
tests/support/temporal.py
tests/unit/test_temporal_orchestration.py
tests/integration/test_temporal_orchestration.py
```

T01 does not need meaningful production Workflows, schedules, database
migrations or agent changes. A test-only Workflow and test-only Activities live
under `tests/support/`, not in the production package.

T01 is accepted only when:

1. Cloud/local configuration validation follows section 6.
2. mTLS client construction uses the mandatory validating Payload Converter,
   encrypted Data Converter and header Codec; injected providers are refused
   outside `dev/test`.
3. Codec round-trip, tamper, invalid key, unknown version and plaintext-fallback
   refusal tests pass.
4. Control-contract validation rejects every forbidden category, oversized
   complete collection and excessive nesting on every serialization path.
5. Workflow ID builders produce the exact forms in section 9.
6. Retry policy loaders enforce the ceilings in section 12.
7. Worker factory applies the queue, deployment/build ID, pinned versioning and
   concurrency for each role.
8. The Temporal test server executes without a skip fallback and proves
   encrypted Workflow input/result, Signal, Activity and failure history
   contains no seeded PII sentinel.
9. No production package imports from `spikes/`.
10. Existing repository tests remain green.
