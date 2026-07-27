# ADR-001 — Durable workflow orchestration

- **Status:** accepted for implementation; Cloud go-live evidence pending
- **Decision date:** 2026-07-24
- **Implementation approval:** CTO/owner, 2026-07-24
- **Owners:** Pacha CTO and repository owner
- **Supersedes:** Celery/Redis as the intended permanent agent-workflow runtime

## Context

Pacha needs workflows that survive worker loss, long waits, human decisions,
retries, deployment changes and temporary control-plane unavailability. The
claims database, event records and audit ledger must remain authoritative, and
external actions still require Pacha's autonomy, idempotency, readback and
reconciliation controls.

The existing Celery/Redis runner, stale-run reaper and Beat timers are already
implemented in part, but Pacha is not live. There is no production workload to
shadow or migrate, so a dual-runtime period would add risk without protecting
users.

## Decision

Temporal Cloud is the selected durable workflow engine. The CTO/owner approved
implementation after technical, privacy, operations and procurement review.
Pacha will not self-host Temporal.

Temporal is responsible for workflow position and recovery, durable timers,
technical retries, Activity heartbeats, human-input waits through Signals, and
workflow deployment/version compatibility.

Pacha remains responsible for:

- canonical claims and append-only events in PostgreSQL;
- the single-writer append-only audit ledger;
- rules, calculations, evidence and human authority;
- stable idempotency keys;
- the `execute_or_stage` side-effect choke point;
- external-write verification, readback and reconciliation; and
- the final truth about what happened.

`agent_runs` remains a Pacha-owned audit and operational read model. It is
projected from workflow events and Pacha commits; it is not Temporal's source of
truth.

Workflow inputs, results, memo/search attributes and history may contain only
opaque identifiers, hashes, statuses and non-sensitive control data. Claim
documents, customer details, bank details, extracted facts and all other PII
are loaded inside Pacha Activities from authorised stores and are never placed
in Temporal payloads. A client-side Payload Codec/encryption layer is required
as defence in depth, not as permission to place PII in workflow history.

Temporal does not provide exactly-once external effects. An uncertain write is
never retried automatically; it becomes `EXCEPTION{uncertain_write}`.

## Implementation approval and go-live gate

Implementation follows
`docs/architecture/TEMPORAL_IMPLEMENTATION_MASTER_PLAN.md`. T01–T08 replace the
existing development-only Celery/Redis orchestration without a production
dual-run. T09 adds Cloud infrastructure and T10 records go-live evidence.

Go-live requires all of:

1. the isolated reliability spike passes every scenario in
   `docs/architecture/temporal_reliability_report.md`;
2. the exact Temporal Cloud region, latency from the intended AWS region,
   recovery behaviour, history contents, codec approach, cost, SLA and exit
   procedure are evidenced;
3. security and data-protection review accepts the region, DPA/DPIA,
   pseudonymous/encrypted history posture and operational access model;
4. procurement accepts price and terms; and
5. the CTO/repository owner gives explicit go-live approval based on that
   evidence.

Implementation approval does not claim that the local spike measured Cloud
latency, RDS recovery or the final deployed Codec. Those are T10 evidence.

## Fallback

AWS Step Functions is the predetermined fallback if Temporal fails the
reliability trial, no available region passes the DPIA, commercial terms are
unacceptable, or the encrypted/pseudonymous-history requirement cannot be met.
Fallback evaluation must apply the same reliability, privacy, idempotency,
external-write and Postgres-authority requirements. It is not an automatic
switch.

## Consequences

`PACKET-21` and `PACKET-22` are frozen and require reissue. Temporal is
implemented T01→T10. Since Pacha is not live, coding agents migrate the codebase
sequentially and remove the custom runner, reaper, Beat and unused Celery/Redis
dependencies before launch; they do not construct a runtime selector.
