# Runbook — Temporal system Workflows

Covers the four finite system Workflows delivered by TEMPORAL-T02:
`OutboxDrainWorkflow`, `LedgerDrainWorkflow`, `SlaEvaluationWorkflow` and
`LedgerVerificationWorkflow`.

## 1. Purpose and authority boundaries

Temporal is **orchestration and recovery only**. It decides *when* work runs and
*that it runs again after a crash*. It decides nothing about a claim.

PostgreSQL remains authoritative for every fact:

| Question | Where the answer lives |
|---|---|
| What is this claim's state? | `claims`, `claim_fields` |
| What happened, in order? | `events` |
| What has been audited? | `audit_ledger` |
| What needs a human? | `review_items` |
| Where is this run's Workflow? | `agent_runs` (a **projection**) |
| Is a Workflow currently executing? | Temporal |

Temporal is **not** a claim store, event store, audit ledger, review authority or
console read model. `agent_runs` is written *from* Temporal observations; it is
never read back into claim truth, and a Temporal Query is never an operations
data source. If Temporal and PostgreSQL disagree about a domain fact,
PostgreSQL is right and the projection is stale.

The system Workflows contain no business logic. Each one invokes a bounded
Activity that calls an existing, already-idempotent Pacha service.

## 2. Queues

Two Task Queues are involved. Role determines queue, concurrency and Deployment
name, so a Worker cannot poll a queue its role does not own.

| Queue | Role concurrency | Registers |
|---|---|---|
| `pacha-{env}-control-v1` | 20 | all four Workflows, plus `dispatch_nonledger_events`, `evaluate_slas`, `record_agent_run_started`, `record_agent_run_status` |
| `pacha-{env}-ledger-v1` | 1 | no Workflows; exactly `append_ledger_batch` and `verify_ledger` |

All four Workflows *run* on the control queue. `LedgerDrainWorkflow` and
`LedgerVerificationWorkflow` schedule their Activities onto the ledger queue,
deriving the name from their own Task Queue (`…-control-v1` → `…-ledger-v1`) and
refusing any queue that is not a control queue.

The ledger Worker's concurrency of **one**, plus the PostgreSQL advisory lock in
§7, is what enforces the PRD-00 single-writer rule. Never register a ledger
Activity on the control queue, and never raise the ledger role's concurrency.

## 3. Maximum batch and execution sizes

| Bound | Value |
|---|---|
| Delivery rows per Activity batch | 50 |
| Batches per Workflow execution | 10 |
| **Delivery-row attempts per drain execution** | **500** |

A drain Workflow stops early the moment a batch attempts fewer than 50 rows,
which means the backlog is empty. Reaching the ten-batch cap is a *normal*
outcome, not a failure: the Workflow returns `completed` and the next scheduled
execution picks up the remainder. Both drains are finite by construction — there
is no `continue_as_new`, no timer and no unbounded loop.

`SlaEvaluationWorkflow` and `LedgerVerificationWorkflow` invoke exactly one
Activity each and return its result.

> T07 owns Schedules. Until T07 lands there is **no** cadence: these Workflows
> only run when something starts them.

## 4. Inspecting backlog without reading claim facts from Temporal

Backlog lives in PostgreSQL, not in Workflow history. Query the database:

```sql
-- Outstanding work per consumer.
SELECT consumer, status, COUNT(*)
FROM event_deliveries
GROUP BY consumer, status
ORDER BY consumer, status;
```

```sql
-- The oldest un-delivered events, by outbox order. Ids only, no payloads.
SELECT d.consumer, d.status, d.attempts, e.seq, e.type
FROM event_deliveries d
JOIN events e ON e.id = d.event_id
WHERE d.status NOT IN ('succeeded', 'dead_letter')
ORDER BY e.seq, d.consumer
LIMIT 50;
```

Healthy: `pending`/`failed` counts stay small and `seq` keeps advancing.

Do **not** read claim facts out of Temporal to answer an operational question.
Workflow history carries opaque references only — there is nothing in it to
read. Use the console and the claim APIs, which read PostgreSQL.

## 5. Identifying dead letters

A delivery that fails 8 times is marked `dead_letter` and emits one `ops.alert`
event. The domain event itself is never deleted.

```sql
SELECT occurred_at, payload
FROM events
WHERE type = 'ops.alert'
  AND payload ->> 'subtype' = 'event_delivery_dead_letter'
ORDER BY seq DESC
LIMIT 20;
```

The payload names `event_id`, `failed_consumer` and `attempts`. To see why:

```sql
SELECT consumer, attempts, last_error
FROM event_deliveries
WHERE status = 'dead_letter'
ORDER BY consumer;
```

An `ops.alert` caused by consumer X is deliberately invisible to consumer X, so
a failing consumer cannot dead-letter its own alert in a loop.

## 6. Rerunning a finite system Workflow safely

All four are safe to rerun at any time. Each drives an idempotent service, and
progress is recorded in `event_deliveries` / `audit_ledger` rather than in
Workflow state, so a rerun resumes rather than repeats.

Start one with a fresh Workflow ID from the appropriate `pacha.{kind}.{ULID}`
form on `pacha-{env}-control-v1`. Rerunning is the correct response to a
transient backlog; it is never a substitute for investigating a dead letter.

### Why duplicate starts and Signals are safe

* **Starts.** Every start pins `WorkflowIDReusePolicy.REJECT_DUPLICATE` *and*
  `WorkflowIDConflictPolicy.USE_EXISTING`, and the Workflow ID is derived from a
  Pacha ULID that was committed **before** the start was attempted. A retried
  start therefore attaches to the execution that ULID already identifies instead
  of creating a second one. No domain work is duplicated.
* **Signals.** A Signal carries exactly one opaque `event_ref`. The receiving
  Workflow keeps a bounded set of references it has already seen and drops a
  repeat, and the Activity it calls applies the event idempotently against
  PostgreSQL. Both layers matter: Workflow memory is lost on Continue-As-New,
  the database row is not.
* **Outbox success is written only after the SDK acknowledges.** If Pacha
  crashes between the SDK call and the success write, the delivery stays
  retryable and the next drain repeats it — landing on the two de-duplication
  layers above. This is deliberate: at-least-once delivery onto idempotent
  application, never at-most-once.

## 7. Ledger advisory locking

Two things make the audit ledger single-writer:

1. the ledger Worker role runs with **Activity concurrency 1**;
2. every append takes a PostgreSQL transaction-scoped advisory lock:

```sql
SELECT pg_advisory_xact_lock(hashtext('pacha:audit-ledger-writer'));
```

The lock is transaction-scoped, so it is released on commit or rollback — there
is nothing to unlock manually and no lock to leak on a Worker crash. On SQLite
(local development and tests) a process-local `threading.Lock` stands in.

To see a contended lock in production:

```sql
SELECT pid, state, wait_event_type, wait_event, query_start
FROM pg_stat_activity
WHERE wait_event_type = 'Lock'
ORDER BY query_start;
```

Brief waits during a drain batch are normal. Sustained waits mean more than one
ledger writer is running — check for a second ledger Worker or a raised
concurrency setting.

`LedgerWriter._append` is the only code path authorised to insert an
`audit_ledger` row, and `append_ledger_batch` reaches it only through the
dispatcher's `ledger` consumer. Never insert an audit row by any other route.

## 8. Audit-degraded mode

`verify_ledger` recomputes the whole hash chain nightly. When the chain verifies,
the head is anchored to the immutable store and the Activity returns `completed`.

When it does **not** verify, the service has already, inside the same
transaction:

* set platform state `audit_degraded = true`;
* set `autonomy_promotions_frozen = true` — no capability may be promoted while
  the audit trail is untrustworthy;
* emitted `ops.alert{subtype: audit_chain_verification_failed}` carrying the
  first bad sequence number.

The Activity then fails the Workflow with the sanitised classification
`payload_diverged`, so the failure is visible in Temporal without any hash,
sequence number or row content entering Workflow history.

**Audit-degraded mode is a stop-and-escalate condition, not a retry condition.**
Do not rerun the verification hoping for a different answer, and do not clear the
flag to unblock promotions. Escalate per §10.

## 9. Behaviour during a Temporal outage

`create_app()` does not connect to Temporal, and no FastAPI request path calls
it. With Temporal completely unavailable:

| Still works | Pauses |
|---|---|
| Claim create, read, update, transition | Outbox drain to Temporal |
| Document upload, timeline, event replay | SLA evaluation |
| Console reads | Ledger append batches |
| Every PostgreSQL write and its committed event | Nightly ledger verification |

Nothing is lost. Committed events stay in `events`, undelivered rows stay in
`event_deliveries`, and asynchronous progress resumes when Temporal returns —
the backlog drains 500 rows per execution.

If the outage is long enough to build a large backlog, expect several drain
executions before `event_deliveries` is clear. That is the design, not a fault.

## 10. Escalation criteria

Escalate to the platform owner when any of these holds:

| Condition | Why it matters |
|---|---|
| `event_deliveries` backlog grows across consecutive drain executions | Throughput is below inflow; 500 rows/execution is not keeping up |
| Any row reaches `dead_letter` | Eight attempts failed; an event will never be delivered without intervention |
| `ops.alert{audit_chain_verification_failed}` | The audit chain diverged. Audit-degraded mode is on and promotions are frozen |
| `LedgerVerificationWorkflow` fails with `payload_diverged` | Same event, seen from Temporal |
| Codec failure (`CodecError`, KMS `GenerateDataKey`/`Decrypt` errors) | There is no plaintext fallback by design; every affected payload is refused, so Workflows cannot start or progress |
| Sustained advisory-lock contention on `pacha:audit-ledger-writer` | A second ledger writer exists, breaking the single-writer invariant |
| A drain Workflow fails rather than returning `completed` | Either an infrastructure error escaped the dispatcher, or an Activity returned a status outside the contract |

## 11. T02 limitations

Deliberate, and not defects to be worked around:

* **No Schedules.** T07 owns schedule creation and cadence. Nothing runs these
  Workflows automatically yet.
* **No production business mappings.** `TEMPORAL_INTENT_MAPPINGS` is empty.
  T02 ships no production business Workflow, so there is nothing for a start or
  Signal to reach; `review.resolved` routing is proved by a test-only mapping.
  T03 adds the first production mapping alongside `DocumentChaseWorkflow`.
* **No deployed Worker process.** T09 owns deployed Workers; T02 constructs them
  explicitly in tests.
* **Celery and Redis remain.** T08 removes them once every replacement is green.

## 12. Prohibited operations

* Do not edit `event_deliveries` rows by hand to "clear" a backlog or a dead
  letter. The row is the delivery record; rewriting it destroys the evidence of
  what was and was not delivered.
* Do not mutate, reset or hand-edit Workflow history.
* Do not terminate a system Workflow to make a backlog disappear — it will
  simply be undrained.
* Do not clear `audit_degraded` without owner sign-off.
