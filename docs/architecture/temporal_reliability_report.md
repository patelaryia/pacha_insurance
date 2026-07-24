# Temporal reliability spike report

**Status:** local mechanics pass; implementation approved; Cloud go-live evidence pending
**Evidence date:** 2026-07-24
**Decision:** T01–T10 implementation authorised; production go-live is not approved

## 1. Executive finding

The isolated Python 3.12 spike demonstrates the core SDK mechanics needed for a
Pacha claim workflow: deterministic recovery on another Worker, Activity
heartbeats, a durable timer, a human Signal wait, duplicate-start suppression,
replay of old history by newer code, encrypted control-only payloads and a
single-attempt governed external write.

The CTO/owner separately approved Temporal for implementation on technical,
privacy, operations and procurement grounds. This local report is still not
evidence that the deployed system is acceptable for production. No
approved Cloud namespace, region, private-connectivity path, AWS-origin test
host, production-like PostgreSQL service, procurement terms or failure-injection
window was supplied. Cloud latency, service-outage recovery, data residency,
commercial cost and the operational support tier were not measured by this
spike. Those are T09/T10 go-live evidence, not blockers to T01–T08 code
implementation.

## 2. What was tested

- Code: `spikes/temporal_reliability/`
- SDK: `temporalio==1.30.0`
- Runtime: Python 3.12
- Service: SDK-downloaded ephemeral Temporal test server on the developer
  machine
- Temporal Cloud region: **not tested**
- AWS source region and network path: **not tested**
- Data: synthetic identifiers and synthetic claim facts only
- Persistence: local SQLite test double for the Pacha authoritative-store
  contract; this is not production PostgreSQL evidence
- Command:
  `python -m pytest -q --durations=0`
- Result: **7 passed in 4.34 seconds**
- Worker interruption/recovery test duration: **2.36 seconds total test wall
  time**. This includes setup and is not a Cloud recovery-time commitment.

## 3. Acceptance matrix

| # | Required property | Result | Evidence or boundary |
|---:|---|---|---|
| 1 | Resume after Worker termination mid-run | Local pass | The first Worker is shut down after an injected Activity interruption; the execution subsequently completes. An OS-process kill in Cloud remains required. |
| 2 | Different Worker resumes | Local pass | A separately constructed Worker polls the same Task Queue and completes the execution. Workflow caching is disabled in the test to make the hand-off explicit. |
| 3 | Wait for human Signal without occupying a Worker | Local pass | The Workflow records `awaiting_review`, waits on an opaque review reference, then resumes from a Signal. The durable wait is in Temporal rather than a Worker thread. |
| 4 | Duplicate trigger does not duplicate work | Local pass | A second start with the same Workflow ID uses the existing execution; the governed external action has one attempt. |
| 5 | Heartbeat recovers interrupted work | Local pass | The retry resumes from recorded heartbeat details and completes the remaining checkpoints. |
| 6 | New Worker version preserves existing Workflow | Local pass | History produced by `DurableClaimWorkflowV1` replays with `DurableClaimWorkflow`; the new path is guarded by a version patch marker. Cloud Worker Deployment behaviour is still untested. |
| 7 | Long timer survives Worker/application restart | Local pass | A 30-day timer is advanced by the test service after Worker replacement and the Workflow completes. Real elapsed-time retention remains a Cloud trial item. |
| 8 | Temporary Temporal access loss pauses safely | Partial | Removing Workers pauses execution while the Pacha-side data remains intact. An actual Temporal Cloud/network outage and reconnect was not available and must be injected in the Cloud trial. |
| 9 | External action only through `execute_or_stage` | Local pass plus source invariant | The Activity invokes the single gate; the spike contains no alternate external-write call path. Repository CI continues to police the production invariant. |
| 10 | Uncertain write is not retried | Local pass | An injected indeterminate response records `uncertain`, returns `blocked`, creates one attempt and leaves the claim uncompleted. |
| 11 | No claim PII in Workflow payload/history | Local pass | Control contracts reject PII-like fields; fetched history is scanned for the synthetic customer name and bank account. Facts are loaded inside Activities from the authoritative store. |
| 12 | Payload encryption/Codec works | Local pass | A client-side AES-256-GCM Payload Codec round-trips SDK payloads; plaintext control identifiers are absent from serialized payloads. Key custody/rotation with AWS KMS is untested. |
| 13 | PostgreSQL remains authoritative | Design pass; environment blocked | Completion and external outcomes are committed by the Pacha-side store, never inferred from history. The executable spike uses SQLite, so production-like RDS PostgreSQL evidence is still required. |
| 14 | Reconstruct `agent_runs` from Workflow events | Local pass | Activity transitions project Workflow progress into the local `agent_runs` read model while claim facts remain separate. Rebuild/load testing against PostgreSQL is pending. |
| 15 | Temporal outage does not block claim reads | Local pass for application separation; Cloud outage blocked | Claim reads continue directly from the authoritative store while no Worker is running. A real Temporal Cloud outage test is required before acceptance. |

## 4. Workflow history and encryption

The Workflow input contains only `run_ref`, `claim_ref`, `trigger_event_ref`,
`payload_hash` and a timer duration. Signals carry only a `review_event_ref`.
Activity commands add only opaque references, step names, stable write IDs and
hashes. Customer details, bank details, documents and extracted facts remain in
the Pacha-side store and are loaded by Activities.

The spike's Payload Codec encrypts every serialized Payload with AES-256-GCM and
fresh nonces, marks the encoding as `binary/encrypted`, and authenticates the
original encoding metadata. Production key material must come from approved KMS
key-management and rotation procedures; the static synthetic test key is not a
production design.

Encryption is defence in depth, not permission to place PII in history. The
control-only schema remains mandatory even when the Codec is enabled.

## 5. Authority, idempotency and failure behaviour

Temporal coordinates when work is eligible to run; it is not the final record
of a claim or an external effect. Activities persist status transitions to the
Pacha-side authoritative store. The governed external action uses a stable
`write:{run_ref}` ID and calls `execute_or_stage`. A known completion can be
read back and reconciled. An indeterminate completion is recorded once and
becomes a blocked review outcome; it is not retried by the Workflow.

The local authority store deliberately models these boundaries but is SQLite
only. The Cloud acceptance run must use a disposable, production-like
PostgreSQL database and prove transactional claim events, the append-only ledger
path and reconstruction of `agent_runs`.

## 6. Operational dependencies and usage/cost

Required before the Cloud trial:

1. Temporal Cloud trial/order and an approved namespace.
2. Exact Temporal region(s) and a DPIA determination for data residency,
   subprocessors, international transfer and retention/deletion.
3. TLS identity and secret distribution from the approved secrets system.
4. An ECS/Fargate Worker in the intended AWS region and approved egress or
   private connectivity.
5. Disposable RDS PostgreSQL, KMS-backed Codec keys, CloudWatch/OpenTelemetry
   telemetry and an agreed failure-injection window.
6. Named service owner, on-call/runbook ownership, support tier, incident path,
   namespace quotas and capacity limits.
7. Procurement review of the order form, DPA, SLA, exit/export capability and
   cost.

The only repository volume input is the launch envelope of 50 claim
intimations/day (open item 156). A planning ceiling of 1,100 Workflow starts per
22-working-day month can therefore be used for an initial quote. The number of
billable Actions per claim, retained-history storage, support tier, networking
and high-availability topology are not yet known. Exact pilot cost is
`blocked_on_inputs`: obtain a Temporal quote and measure Actions per completed
representative claim in the Cloud trial. No price has been invented from a
marketing calculator.

Temporal's current published terms state 99.9% for a standard single-region
deployment and 99.99% for same-region replication or multi-region deployment.
Credits are the stated remedy, availability is measured in five-minute
intervals, exclusions apply, and response times depend on the purchased support
tier. Those contractual limits must be reviewed against Pacha's operational
needs rather than treated as evidence of recovery. Source:
<https://temporal.io/terms-of-service>.

## 7. Cloud trial that remains to be run

The fail-closed entry point is:

```bash
python -m temporal_spike.cloud_trial
```

It requires the namespace endpoint, namespace, exact region, TLS certificate
and key paths, AWS-origin label and report path. Even when those inputs exist,
it stops for owner approval of the external failure-injection window. It does
not infer evidence from credentials and does not accept real claim data.

The approved run must measure and preserve evidence for:

- p50/p95/p99 start, Signal and Activity-scheduling latency from the intended AWS
  region;
- Worker OS-process kill recovery time and heartbeat checkpoint recovery;
- network isolation from Temporal followed by reconnect;
- namespace/service interruption behaviour and claim-read availability;
- duplicate delivery and uncertain external-write outcomes;
- Worker Deployment/version compatibility;
- PostgreSQL and ledger authority/reconstruction;
- history export showing only encrypted, non-sensitive control payloads;
- KMS rotation behaviour, history retention/deletion and access controls;
- observed Actions/storage/egress and a costed pilot projection.

## 8. Go-live gate and fallback

Production go-live is prohibited until all of the following are recorded:

1. every matrix item passes in the approved Cloud/RDS environment;
2. security and data-protection reviewers accept the region, DPA/DPIA,
   encryption, retention, access and incident controls;
3. procurement accepts the commercial terms, support/SLA tier and cost;
4. the CTO/repository owner explicitly approves the go-live evidence.

Implementation proceeds under the Temporal master plan. Because Pacha is not
live, Celery/Redis orchestration is removed before launch rather than operated
as a production shadow engine.

If reliability, region/DPIA, encrypted-history or commercial acceptance fails,
the owner should open a separate AWS Step Functions recommendation. It must use
the same opaque-control-data rule, PostgreSQL authority, `execute_or_stage`
choke point, idempotency, uncertain-write handling, human callback security,
versioning tests and failure matrix. The switch is a reviewed decision, not an
automatic code change. Exit from Temporal requires stopping new starts,
allowing or explicitly resolving open Workflows, exporting execution evidence,
reconciling every run against PostgreSQL and only then removing the namespace
under an approved retention procedure.
