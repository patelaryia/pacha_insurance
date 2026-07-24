# TEMPORAL-T02 — Runtime projection, outbox bridge and finite system Workflows

> **Status:** issued for implementation
> **Builder:** Claude, operating as the coding agent under `AGENTS.md`
> **Reviewer:** CTO / repository owner
> **Depends on:** T00 commit `282112e` and the reviewed T01 Temporal substrate
> merged on top of it
> **Source of truth:** `docs/AGENT_BUILD_GUIDE.md`,
> `docs/Section_0_Shared_Engineering_Decisions_v1.1.md`,
> `docs/Section_0.5_Shared_Agent_Runtime_v1.1.md`, PRD-00, PRD-03, PRD-04,
> and `docs/architecture/TEMPORAL_IMPLEMENTATION_MASTER_PLAN.md` §§2–16,
> §§19, 22–24
> **Open-item authority:** register item 284. If T01 also lands register item
> 285, preserve it unchanged.

## 0. Executive instruction

Implement T02 only.

T02 converts the existing `agent_runs` table into the binding Temporal
operational projection, adds the asynchronous Temporal intent boundary to the
existing transactional outbox, and implements the four finite system
Workflows:

```text
OutboxDrainWorkflow
LedgerDrainWorkflow
SlaEvaluationWorkflow
LedgerVerificationWorkflow
```

T02 does **not** migrate an intake, chase, document-intelligence, assessment,
approval-pack or projection-agent execution. Those are T03–T06. T02 does not
create Temporal Schedules; T07 owns schedule creation and cadence. T02 does not
remove Celery/Redis; T08 owns removal after every replacement is green.

The architectural boundary is:

```text
Pacha transaction
  -> events + event_deliveries commit
  -> API may return without Temporal

finite Temporal system Workflow
  -> bounded Activity
  -> existing Pacha service / dispatcher
  -> PostgreSQL remains authoritative
```

Temporal is orchestration and recovery only. It is not a claim store, event
store, audit ledger, review authority or console read model.

## 1. Preconditions and mandatory preflight

Before editing:

1. Read `AGENTS.md` and every source document named above.
2. Confirm the current branch contains T00:

   ```bash
   git merge-base --is-ancestor 282112e HEAD
   ```

3. Confirm the reviewed T01 files exist and its targeted tests pass:

   ```bash
   test -f platform/orchestration/worker.py
   test -f tests/integration/test_temporal_orchestration.py
   pytest -q tests/unit/test_temporal_orchestration.py
   pytest -q tests/integration/test_temporal_orchestration.py
   ```

4. Inspect `git status --short`. Preserve all unrelated and user-owned
   changes. In particular, do not stage, edit or delete an unrelated
   `docs/packets/PACKET-22_live_operation_activation.md`.
5. If T01 is not present and green, stop. Do not recreate or redesign T01
   inside T02.

T02 is one coherent PR. Do not proceed into T03.

## 2. Exact slice boundary

### 2.1 In scope

- Alembic revision `0016_temporal_runtime.py`;
- the exact expanded `agent_runs` ORM model and indexes;
- an `AgentRunProjection` service for pending-row creation and idempotent
  Workflow-state projection;
- compatibility values for the still-present legacy runner;
- bounded synchronous and asynchronous dispatcher entry points;
- `TemporalStarter`, the sole start/Signal transport used by later packets;
- code-owned `TemporalIntentMapping` and `TemporalIntentConsumer`;
- the six standard Signal names and exact one-reference Signal contract;
- a production mapping registry that is deliberately empty in T02;
- system Activities that wrap the existing dispatcher, SLA engine and ledger;
- the four finite system Workflows;
- explicit control/ledger Worker registration helpers;
- the required ledger advisory-lock correction;
- migration, unit, integration, privacy, idempotency and replay tests;
- a system-Workflow runbook.

### 2.2 Explicitly out of scope

- any production business Workflow;
- any production event-to-business-Workflow mapping;
- changes to chase, intake, document intelligence, assessment, approval pack or
  projection behaviour;
- Temporal Schedules or `schedules.py`;
- FastAPI request-path calls to Temporal;
- Temporal Queries as an operations read source;
- Memo, Search Attributes, custom headers, static summary/details, Updates,
  Nexus or cron starts;
- a new `orchestration_commands` table or any other schema;
- an async database driver or conversion of the repository to async
  SQLAlchemy;
- a generic Workflow DSL, reflection registry or plugin system;
- Celery/Redis removal;
- infrastructure, ECS services, IAM or Temporal Cloud provisioning;
- payment execution, vendor execution, RPA or auction-provider work.

## 3. Required file changes

Add:

```text
platform/orchestration/activities.py
platform/orchestration/starter.py
platform/orchestration/workflows.py
platform/agent_runtime/projection.py
platform/claim_core/alembic/versions/0016_temporal_runtime.py
tests/unit/test_temporal_t02.py
tests/integration/test_temporal_t02.py
tests/fixtures/temporal/t02/outbox_drain.json
tests/fixtures/temporal/t02/ledger_drain.json
tests/fixtures/temporal/t02/sla_evaluation.json
tests/fixtures/temporal/t02/ledger_verification.json
docs/runbooks/temporal_system_workflows.md
```

Modify only as needed:

```text
platform/orchestration/__init__.py
platform/orchestration/contracts.py
platform/agent_runtime/models.py
platform/agent_runtime/runner.py
platform/claim_core/outbox.py
platform/claim_core/ledger.py
tests/conftest.py
tests/support/temporal.py
tests/acceptance/test_packet_13_agent_runtime.py
```

`tests/acceptance/test_packet_13_agent_runtime.py` is a protected path. The only
authorised change is to replace its old `AR1_COLUMNS` expected set with the
expanded binding Section-0.5 set. Do not alter its scenarios or assertions for
legacy runtime behaviour.

Do not add a new production service entry point. T09 owns deployed Worker
processes. T02 integration tests construct Workers explicitly with T01's
`build_worker`.

## 4. Exact `agent_runs` persistence contract

### 4.1 ORM

Change `platform/agent_runtime/models.py` to match this schema exactly:

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

Binding details:

- use the repository's existing JSON/JSONB cross-dialect type;
- use timezone-aware `DateTime`;
- add no foreign key from `claim_id`;
- `workflow_id` has one named unique constraint:
  `uq_agent_runs_workflow_id`;
- retain the check-constraint name `ck_agent_runs_status`;
- declare both indexes in ORM metadata with the exact names above;
- add no created/updated timestamp, attempt counter, lease, schedule or payload
  column.

The complete expected column set is:

```text
id
agent
capability_id
claim_id
trigger_event
workflow_id
workflow_run_id
workflow_type
worker_build_id
status
steps
autonomy_level
error
last_workflow_event_ref
last_synced_at
started_at
ended_at
```

### 4.2 Alembic revision

Create revision:

```python
revision = "0016_temporal_runtime"
down_revision = "0015_projections"
```

Upgrade in this order:

1. Add the six new columns nullable:
   `workflow_id`, `workflow_run_id`, `workflow_type`, `worker_build_id`,
   `last_workflow_event_ref`, `last_synced_at`.
2. Backfill every existing row exactly:

   ```text
   workflow_id = 'pacha.legacy.agent.' || id
   workflow_type = 'LegacyAgentRun'
   worker_build_id = 'legacy-celery'
   ```

3. Make `workflow_id` and `workflow_type` non-null.
4. Replace `ck_agent_runs_status` with the seven-value check.
5. Create `uq_agent_runs_workflow_id`.
6. Create `ix_agent_runs_status` and `ix_agent_runs_claim`.

Use Alembic batch operations where SQLite requires table recreation. Do not
hand-edit SQLite system tables.

Downgrade in exact reverse order. Before restoring the old five-value status
check, explicitly query for `pending` or `cancelled`. If either exists, raise a
clear `RuntimeError` and leave the migration unapplied; do not silently map,
delete or mislabel those rows. Empty databases and databases containing only
legacy statuses must downgrade cleanly on SQLite and PostgreSQL.

Migration tests must prove:

- exact columns, nullability, unique constraint, check and indexes after
  upgrade;
- exact backfill for at least two pre-existing rows;
- uniqueness of `workflow_id`;
- all seven statuses accepted and an unknown status refused after upgrade;
- downgrade succeeds and preserves legacy rows;
- downgrade refuses rather than corrupts a `pending` or `cancelled` row;
- a second upgrade after a clean downgrade succeeds.

### 4.3 Legacy-runner compatibility

T02 does not delete or rewrite `AgentRunner`. Every new row it creates before
its T03–T06 replacement must populate:

```text
workflow_id = "pacha.legacy.agent." + run_id
workflow_type = "LegacyAgentRun"
worker_build_id = "legacy-celery"
workflow_run_id = null
last_workflow_event_ref = null
last_synced_at = null
```

The legacy value is migration metadata, not a legal Temporal Workflow ID. It
must never be passed to `TemporalStarter` or the T01 payload converter.

Keep existing legacy start status `running`; `pending` begins to be used by the
business migration packets when they create a Pacha row before a Temporal
start. Preserve all existing runner tests.

### 4.4 Projection service

Add `platform/agent_runtime/projection.py` and expose
`AgentRunProjection`, `AgentRunNotFound` and `AgentRunConflict` from
`agent_runtime.__init__`.

```python
class AgentRunProjection:
    def __init__(self, app: Any) -> None: ...

    def prepare(
        self,
        session: Session,
        *,
        run_ref: str,
        agent: str,
        capability_id: str,
        autonomy_level: str,
        workflow_ref: WorkflowRef,
        workflow_type: str,
        claim_ref: str | None = None,
        trigger_event_ref: str | None = None,
        step_ids: Sequence[str] = (),
    ) -> None: ...

    def record_started(
        self,
        *,
        run_ref: str,
        workflow_ref: str,
        workflow_run_ref: str,
        workflow_type: str,
        worker_build_id: str,
    ) -> None: ...

    def record_status(self, result: ControlResult) -> None: ...

class AgentRunNotFound(LookupError): ...
class AgentRunConflict(ValueError): ...
```

`prepare` is deliberately session-taking: T03–T06 call it inside the same
transaction that creates the initiating Pacha event. It must not open or commit
another transaction. It:

- validates all opaque references with T01 contracts;
- requires an uppercase ULID `run_ref`;
- requires a non-empty code-owned `agent`, `capability_id` and
  `workflow_type`;
- validates every `step_id` against the T01 pack registry and refuses
  duplicates;
- inserts `status="pending"`;
- builds steps as
  `{step_id, status: "pending", attempts: 0, updated_at}` in declared order;
- snapshots the supplied autonomy level;
- sets `started_at` from `app.state.clock()`;
- sets all Worker/run/sync/error/end fields null;
- flushes but never commits;
- lets unique/FK failures propagate.

`record_started` owns its transaction and row-locks on PostgreSQL. It verifies
the row's Workflow ID and type against the Activity's actual Workflow info.
`pending -> running` is legal. Repeating the same observation is idempotent.
A different Workflow Run ID for the same Workflow ID is accepted only while
the row is active (`running` or `awaiting_review`) because Continue-As-New
changes Run ID. Any Workflow-ID/type mismatch or attempt to restart a terminal
row raises `AgentRunConflict`. A missing row raises `AgentRunNotFound`. It sets
`worker_build_id`, `workflow_run_id`,
`last_synced_at`, and clears neither steps nor domain error detail.

`record_status` owns its transaction and applies only:

```text
pending         -> running | failed | cancelled
running         -> awaiting_review | blocked | completed | failed | cancelled
awaiting_review -> running | blocked | completed | failed | cancelled
same status     -> same status (idempotent)
```

`blocked`, `completed`, `failed` and `cancelled` are terminal and set
`ended_at`; `pending`, `running` and `awaiting_review` leave it null. A terminal
row cannot leave or change terminal state. When the result supplies
`event_ref` or `review_event_ref`, store that value as
`last_workflow_event_ref`; always update `last_synced_at`. Do not put a Temporal
Query result into this table and do not overwrite `steps` or `error` in this
generic method. Domain-specific Activities in T03–T06 own step/error detail
after their Pacha commits.

## 5. Dispatcher evolution and batch semantics

Extend `platform/claim_core/outbox.py` without replacing the existing
dispatcher.

### 5.1 Consumer types

A consumer may be either:

```python
Callable[[Event], None]
Callable[[Event], Awaitable[None]]
```

The existing synchronous `dispatch_once` must reject an awaitable result with a
clear `TypeError`; it must never create and abandon an un-awaited coroutine.
Async consumers are driven only by the new asynchronous method.

### 5.2 Public methods

The exact compatible surface is:

```python
def dispatch_once(
    self,
    consumers: Iterable[str] | None = None,
    *,
    limit: int | None = None,
) -> int: ...

async def dispatch_once_async(
    self,
    consumers: Iterable[str] | None = None,
    *,
    limit: int | None = None,
) -> int: ...
```

Rules:

- `limit=None` preserves existing unbounded behaviour;
- a supplied limit must be an integer from 1 through 500; booleans are refused;
- the limit counts claimed delivery rows attempted, not source events and not
  successful consumers;
- candidate ordering is globally `(events.seq, consumer_name)`, not grouped by
  consumer registration order;
- PostgreSQL continues to use `FOR UPDATE SKIP LOCKED`;
- SQLite continues to use the process lock;
- both entry points share the same instance-level dispatch exclusion so they
  cannot concurrently drive the same SQLite dispatcher;
- the async entry point moves synchronous SQLAlchemy/consumer work off the
  event loop with `asyncio.to_thread`;
- the async entry point awaits async consumers directly;
- success is written only after the consumer returns/awaits successfully;
- failure retains the existing retry/dead-letter behaviour and `ops.alert`;
- an `ops.alert` caused by consumer X remains invisible to consumer X;
- no event is deleted.

Do not change `MAX_ATTEMPTS`, retry timing or the `event_deliveries` schema.

## 6. `TemporalStarter`: the only start and Signal transport

Add `platform/orchestration/starter.py`.

### 6.1 Closed Signal registry

Define exactly:

```python
STANDARD_SIGNAL_NAMES = frozenset({
    "pacha_event",
    "review_resolved",
    "claim_terminal",
    "document_received",
    "snooze_changed",
    "inbound_received",
})
```

### 6.2 Public class

```python
class TemporalStarter:
    def __init__(self, client: Client, config: TemporalConfig) -> None: ...

    async def start(
        self,
        *,
        workflow_type: str | type,
        workflow_ref: WorkflowRef,
        command: ControlCommand,
    ) -> None: ...

    async def signal(
        self,
        *,
        workflow_ref: WorkflowRef,
        signal_name: str,
        signal: ControlSignal,
    ) -> None: ...
```

Rules:

- `start` always targets `config.task_queue("control")`;
- it always sets `REJECT_DUPLICATE` and `USE_EXISTING` explicitly;
- it supplies no Memo, Search Attributes, headers, static text, cron or other
  forbidden option;
- it returns only after the SDK acknowledges the start, but it never waits for
  Workflow completion;
- `signal` refuses names outside `STANDARD_SIGNAL_NAMES`;
- Signals contain exactly `ControlSignal(event_ref=...)`;
- `signal` gets the handle by the exact Workflow ID and waits for SDK
  acknowledgement;
- neither method catches and disguises Temporal transport failure;
- neither method writes Pacha rows; Pacha commits before it is called;
- neither method exposes a Workflow handle to a domain package;
- no Query, Update, terminate or cancel method is added.

Add only `TemporalStarter` to the lazy public exports in
`orchestration.__init__`. Preserve the T01 lazy-import guarantee: importing
`orchestration.contracts`, `.errors`, `.ids` or `.policies` must still not
import client, Codec, config, worker, starter, activities or workflows.

## 7. Intent mapping and review Signal routing

Implement in `starter.py`:

```python
@dataclass(frozen=True, slots=True)
class TemporalIntentMapping:
    event_type: str
    workflow_type: str | type
    workflow_id_builder: Callable[[Event], WorkflowRef | None]
    action: Literal["start", "signal"]
    signal_name: str | None
    control_contract_type: type[ControlCommand] | type[ControlSignal]

class TemporalIntentConsumer:
    def __init__(
        self,
        starter: TemporalStarter,
        mappings: Sequence[TemporalIntentMapping],
    ) -> None: ...

    async def __call__(self, event: Event) -> None: ...
```

Construction validation:

- event types are non-empty and unique;
- `workflow_type` is either a non-empty registered type name or a decorated
  Workflow class; no wildcard is accepted;
- action is exactly `start` or `signal`;
- `start` requires `signal_name is None` and
  `control_contract_type is ControlCommand`;
- `signal` requires a standard Signal name and
  `control_contract_type is ControlSignal`;
- `workflow_id_builder` is callable;
- unknown mapping keys or reflection/discovery are impossible because mappings
  are Python dataclass instances declared in code.

Consumption:

- an unknown event type returns normally and is therefore marked succeeded;
- a builder returning `None` means “event is valid but has no Temporal target”
  and returns normally;
- invoke the synchronous builder through `asyncio.to_thread` so a
  database-backed resolver cannot block the Activity event loop;
- a builder exception propagates so the delivery retries/dead-letters;
- `start` constructs only:

  ```python
  ControlCommand(
      run_ref=event.correlation_id,
      claim_ref=event.claim_id,
      trigger_event_ref=event.id,
      event_ref=event.id,
  )
  ```

  and refuses the mapping if `correlation_id` is not a ULID;
- `signal` constructs only `ControlSignal(event_ref=event.id)`;
- delivery success remains owned by `Dispatcher`, after SDK acknowledgement.

### 7.1 Deliberate T02 production-registry decision

Define:

```python
TEMPORAL_INTENT_MAPPINGS: tuple[TemporalIntentMapping, ...] = ()
```

This is intentional and must not be treated as a TODO. T02 has no production
business Workflow to receive a start or Signal. Registering
`review.resolved -> LegacyAgentRun`, inventing a wildcard workflow type, or
signalling a nonexistent Workflow would be incorrect.

T02 proves `review_resolved` routing with a test-only mapping and test-only
Workflow under `tests/support/temporal.py`. T03 adds the first production
mapping at the same time as `DocumentChaseWorkflow`. T04–T06 extend the
code-owned tuple alongside their actual Workflow types.

Do not derive a Workflow ID from review free text, actor, claim facts or
Temporal visibility.

## 8. System Activities

Add `platform/orchestration/activities.py`.

Use one explicitly constructed object:

```python
class SystemActivities:
    def __init__(self, app: Any) -> None: ...

    @activity.defn(name="dispatch_nonledger_events")
    async def dispatch_nonledger_events(self) -> ControlResult: ...

    @activity.defn(name="append_ledger_batch")
    async def append_ledger_batch(self) -> ControlResult: ...

    @activity.defn(name="evaluate_slas")
    async def evaluate_slas(self) -> ControlResult: ...

    @activity.defn(name="verify_ledger")
    async def verify_ledger(self) -> ControlResult: ...

class AgentRunActivities:
    def __init__(
        self,
        projection: AgentRunProjection,
        *,
        worker_build_id: str,
    ) -> None: ...

    @activity.defn(name="record_agent_run_started")
    async def record_agent_run_started(self, command: ControlCommand) -> ControlResult: ...

    @activity.defn(name="record_agent_run_status")
    async def record_agent_run_status(self, result: ControlResult) -> ControlResult: ...
```

Constructor requirements:

- `app.state.dispatcher`, `.sla_engine` and `.ledger` must exist;
- no global application singleton;
- do not build a Temporal client inside an Activity;
- the caller registers a `TemporalIntentConsumer` on the dispatcher before
  constructing the control Worker;
- fail construction if `temporal_intent` is absent from
  `dispatcher.consumer_names`.

### 8.1 `dispatch_nonledger_events`

- call `dispatcher.dispatch_once_async` with every registered consumer except
  `ledger`, sorted by name, `limit=50`;
- this includes `temporal_intent` and the existing domain projection
  consumers;
- return `ControlResult(status="running")` when exactly 50 delivery rows were
  attempted, otherwise `ControlResult(status="completed")`;
- a per-consumer error is already persisted by the dispatcher and does not
  leak into Workflow history;
- an infrastructure error escaping the dispatcher is recorded through the
  repository's diagnostic boundary and re-raised only as
  `sanitised_application_error("activity_internal")`.

### 8.2 `append_ledger_batch`

- call the existing dispatcher for consumer `ledger` only with `limit=50`;
- run the synchronous dispatcher call via `asyncio.to_thread`;
- return `running` at 50, otherwise `completed`;
- this Activity is registered only on the ledger Task Queue;
- do not call `LedgerWriter.consume` directly and do not insert
  `audit_ledger` rows anywhere else.

### 8.3 `evaluate_slas`

- call the existing `SlaEngine.evaluate()` once through `asyncio.to_thread`;
- return `ControlResult(status="completed")`;
- do not pass the wall clock through Workflow history. The Activity/service
  reads the authoritative injected application clock.

### 8.4 `verify_ledger`

- call `LedgerWriter.run_nightly_verification()` once through
  `asyncio.to_thread`;
- a healthy report returns `ControlResult(status="completed")`;
- an unhealthy report has already entered audit-degraded mode and emitted
  `ops.alert`; raise `sanitised_application_error("payload_diverged")` so the
  scheduled Workflow fails visibly without leaking hashes or raw detail;
- unexpected failures become sanitised `activity_internal`.

All Activity inputs are empty and all Activity results are `ControlResult`.
Claim data, event payloads, ledger content and verification hashes never enter
Workflow history.

The final sentence above applies to the four `SystemActivities`. The two
`AgentRunActivities` take the exact control contracts shown and return
control-only results.

### 8.5 Agent-run projection Activities

`record_agent_run_started` requires `command.run_ref` and obtains the actual
Workflow ID, Run ID and Workflow type from `activity.info()`. It calls
`AgentRunProjection.record_started` through `asyncio.to_thread`, using the
constructor's immutable build ID, and returns
`ControlResult(status="running", run_ref=...)`.

`record_agent_run_status` requires `result.run_ref`, calls
`AgentRunProjection.record_status` through `asyncio.to_thread`, and returns the
same result.

Catch internal projection conflicts and raise only sanitised
`idempotency_conflict`; a missing run becomes sanitised `domain_rejected`;
unexpected failures become sanitised `activity_internal`. No raw SQL/exception
text enters the Temporal failure.

## 9. Ledger single-writer correction

In `platform/claim_core/ledger.py`, replace the PostgreSQL advisory transaction
lock key with the exact owner-approved value:

```sql
SELECT pg_advisory_xact_lock(hashtext('pacha:audit-ledger-writer'));
```

Keep the existing process-local `Lock` as the SQLite analogue. Add a source
assertion for the exact key. Do not introduce a second ledger writer.

## 10. Finite system Workflows

Add `platform/orchestration/workflows.py`. The module may import
`temporalio.workflow` and the four deterministic pass-through modules only. It
must not import application services, SQLAlchemy, `activities.py`, config,
client, Codec, worker, agent packages or `orchestration` package root.

Register exact Workflow type names and pinned versioning:

```python
@workflow.defn(
    name="OutboxDrainWorkflow",
    versioning_behavior=VersioningBehavior.PINNED,
)
class OutboxDrainWorkflow: ...
```

Repeat for the other three exact names.

Every `run` method takes no arguments and returns `ControlResult`. No system
Workflow has a Signal or Query handler.

### 10.1 Activity invocation

Call Activities by their registered string names. Load T01 Activity policies
from `load_retry_policies()`:

| Workflow | Activity | Task Queue | Policy |
|---|---|---|---|
| `OutboxDrainWorkflow` | `dispatch_nonledger_events` | current/control | `db_control` |
| `LedgerDrainWorkflow` | `append_ledger_batch` | derived ledger | `ledger_append` |
| `SlaEvaluationWorkflow` | `evaluate_slas` | current/control | `db_control` |
| `LedgerVerificationWorkflow` | `verify_ledger` | derived ledger | `ledger_append` |

For ledger Activities, derive the queue deterministically from
`workflow.info().task_queue`:

```text
pacha-{env}-control-v1 -> pacha-{env}-ledger-v1
```

Refuse any current queue that does not end in `-control-v1`. Do not import
environment variables or `TemporalConfig` inside Workflow code.

### 10.2 Drain loops

Both drain Workflows:

1. invoke their batch Activity;
2. stop early when it returns `status="completed"`;
3. continue only when it returns `status="running"`;
4. refuse any other status as a deterministic contract error;
5. invoke at most ten batches;
6. return `ControlResult(status="completed")` whether the backlog emptied
   early or reached the ten-batch cap.

This gives an absolute maximum of 500 delivery-row attempts per execution.
Remaining backlog is handled by the next T07 Schedule execution.

The SLA and ledger-verification Workflows invoke one Activity and return its
result.

Do not add timers, `continue_as_new`, randomness, wall-clock calls, logging of
payloads, patch markers with no behaviour change, or exception text.

### 10.3 Explicit registration helpers

Expose from the internal modules, but not from `orchestration.__init__`:

```python
SYSTEM_WORKFLOWS = (
    OutboxDrainWorkflow,
    LedgerDrainWorkflow,
    SlaEvaluationWorkflow,
    LedgerVerificationWorkflow,
)

def control_activity_registrations(
    system: SystemActivities,
    agent_runs: AgentRunActivities,
) -> tuple[Callable, ...]:
    return (
        system.dispatch_nonledger_events,
        system.evaluate_slas,
        agent_runs.record_agent_run_started,
        agent_runs.record_agent_run_status,
    )

def ledger_activity_registrations(system: SystemActivities) -> tuple[Callable, ...]:
    return (system.append_ledger_batch, system.verify_ledger)
```

The control Worker registers all four Workflows plus the four control
Activities. The ledger Worker registers no Workflows and exactly the two
ledger Activities. Do not register ledger Activities on control as a test
shortcut.

## 11. Application and deployment wiring

T02 must preserve this rule:

```text
create_app() does not connect to Temporal
```

Claim create/read/update APIs remain fully usable with Temporal stopped.

Tests and the future Worker process wire T02 in this order:

1. build the ordinary Pacha app and its existing services;
2. build one T01 Temporal client;
3. construct `TemporalStarter`;
4. construct `TemporalIntentConsumer(starter, TEMPORAL_INTENT_MAPPINGS)`;
5. register it once as dispatcher consumer `temporal_intent`;
6. construct `SystemActivities(app)`;
7. construct `AgentRunProjection(app)` and
   `AgentRunActivities(projection, worker_build_id=config.build_id)`;
8. construct control and ledger Workers with T01 `build_worker` and the exact
   registrations above.

Do not call `configure_runtime` differently and do not create Schedules.

## 12. Test contract

### 12.1 Unit tests

`tests/unit/test_temporal_t02.py` must cover at least:

1. exact `agent_runs` model columns, check, unique constraint and indexes;
2. all legacy runner-created rows receive the four legacy metadata values;
3. pending preparation occurs in the caller's transaction and rollback leaves
   no row;
4. projection start/status transitions, Continue-As-New Run-ID update,
   idempotency and terminal refusal;
5. dispatcher limit validation, global ordering and backwards compatibility;
6. synchronous dispatch rejects an async consumer without leaking a coroutine;
7. asynchronous dispatch awaits an async consumer and marks success only after
   acknowledgement;
8. async consumer failure increments/retries/dead-letters exactly as before;
9. mapping construction rejects every invalid start/Signal combination;
10. unknown events and `None` targets are acknowledged no-ops;
11. start command contains only the four specified opaque references;
12. Signal contains exactly one event ULID;
13. `TemporalStarter` fixes control queue and both duplicate policies;
14. all six standard Signal names accepted and any other name refused;
15. each system Activity calls only its specified existing service;
16. batches return `running` at exactly 50 and `completed` at 49;
17. unhealthy ledger verification becomes sanitised `payload_diverged`;
18. Workflow source contains no database, app, config, Codec or Activity-module
    import;
19. exact advisory-lock key and no second `audit_ledger` insert path;
20. package-root lazy import remains intact.

Use fakes for unit tests. Do not mock away the integration tests below.

### 12.2 Temporal integration tests

`tests/integration/test_temporal_t02.py` must use the mandatory Temporal test
server with no skip/importorskip fallback and prove:

1. all four production system Workflows register and execute;
2. control Activities run on control and ledger Activities run on ledger;
3. 49 eligible deliveries produce one batch;
4. 501 eligible deliveries produce exactly 500 attempts in one drain
   execution and the next execution handles the remainder;
5. a Worker stop between Activity attempts allows a compatible Worker to
   continue without duplicate ledger rows;
6. duplicate starts attach through `USE_EXISTING`;
7. a test-only `review.resolved` mapping Signals a test-only waiting Workflow;
8. the same event reference delivered twice is de-duplicated by that test
   Workflow and its idempotent Pacha application;
9. a simulated SDK failure leaves the `temporal_intent` delivery retryable and
   does not mark success;
10. Signal success is marked only after SDK acknowledgement;
11. an unknown event type performs no Temporal call and is marked succeeded;
12. fetched histories contain none of the seeded PII/money sentinel values and
    contain only T01 control-contract fields;
13. the production mapping registry is empty;
14. no Temporal skip exists anywhere in the T01/T02 Temporal suites.

The test-only waiting Workflow and fixtures belong under `tests/support/`.
They are not exported or registered by production code.

### 12.3 Database migration tests

Run the migration contract on:

- SQLite in the normal test suite;
- PostgreSQL in the repository's PostgreSQL CI tier.

Do not mark a required PostgreSQL test as an optional runtime skip. Use the
existing tier marker/fixture policy.

### 12.4 Replay

Persist the four history JSON fixtures at the exact paths listed in section 3
after the integration run, using the existing T01 history/privacy support and
the test Codec key. Replay each fixture against the T02 Workflow classes with
the same test Data Converter. A Workflow code change that alters commands in a
later packet requires a patch marker or a new type.

Do not put secrets, PII sentinels or Codec plaintext in committed fixtures.

## 13. Runbook

Create `docs/runbooks/temporal_system_workflows.md` with:

- purpose and authority boundaries;
- control and ledger queues;
- maximum batch/execution sizes;
- how to inspect `event_deliveries` backlog without reading claim facts from
  Temporal;
- how to identify dead-letter `ops.alert`;
- how to rerun a finite system Workflow safely;
- why duplicate starts and Signals are safe;
- how ledger advisory locking works;
- what audit-degraded mode means;
- Temporal outage behaviour: APIs and PostgreSQL reads continue, asynchronous
  progress pauses;
- T02 limitation: no Schedules and no production business mappings until later
  packets;
- escalation criteria for persistent backlog, dead letters, ledger divergence
  and Codec failure.

Do not document manual mutation of Workflow history or direct edits to
`event_deliveries`.

## 14. Acceptance gates

T02 is complete only when all are true:

- T01 remains green without weakened tests;
- `agent_runs` matches Section 0.5 exactly;
- migration upgrade/backfill/downgrade is proven on SQLite and PostgreSQL;
- claim APIs work with no Temporal server;
- every start uses the T01 duplicate policy;
- outbox success occurs only after SDK acknowledgement;
- unknown events are ignored deterministically;
- no production business mapping or Workflow has slipped into T02;
- system Workflows are finite and capped at 500 attempts;
- ledger append/verification run only on the ledger queue;
- the exact PostgreSQL advisory lock is present;
- every Workflow history is control-only and encrypted;
- no Temporal test skips;
- no new direct external-write path;
- no new payment operation;
- no Celery compatibility wrapper around Temporal;
- the full repository remains green.

Run and report exact results:

```bash
ruff check .
python tools/ci/money_float_lint.py
python tools/ci/banned_calls.py
git diff --check
pytest -q tests/unit/test_temporal_orchestration.py tests/unit/test_temporal_t02.py
pytest -q tests/integration/test_temporal_orchestration.py tests/integration/test_temporal_t02.py
pytest -q
pytest -q spikes/temporal_reliability
```

Also report:

```bash
git status --short
git diff --stat
git diff --name-only
```

Do not commit, push or start T03 unless the repository owner explicitly asks.

## 15. Required hand-off format

Return:

1. outcome in one sentence;
2. exact changed-file list;
3. migration evidence for SQLite and PostgreSQL;
4. unit, integration, replay, privacy and full-suite results with pass/skip
   counts;
5. confirmation that the production intent mapping registry is empty;
6. confirmation that no schedule or business Workflow was added;
7. any open item or blocker;
8. `git status --short`;
9. explicit statement: `T03 not started`.

Do not claim production readiness. T09/T10 remain the Cloud/RDS and go-live
evidence gates.
