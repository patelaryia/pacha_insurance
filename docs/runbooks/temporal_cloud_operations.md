# Runbook — Temporal Cloud Workers

This runbook covers T09 deployment, outage response, rollback, stuck Workflows,
uncertain writes and Payload Codec failures. PostgreSQL remains claim truth;
Temporal remains orchestration and recovery only.

## Deployment prerequisites

Do not start a deployment until all of these are attached to the change:

1. reviewed Terraform plan from the environment root;
2. immutable application and ADOT image digests;
3. full application git SHA matching `PACHA_BUILD_ID`;
4. owner-approved namespace/region and Temporal endpoint CIDRs;
5. mTLS certificate/key secret ARNs and immutable Codec KMS key ARN;
6. private subnet, RDS, VPC endpoint, S3 and IAM resource identifiers;
7. a code-owned dependency factory included in the application image;
8. alarm topic and on-call ownership; and
9. a previous immutable build retained for rollback.

Credentials and PEM bytes never appear in plans, task environment variables,
logs, tickets or this runbook.

## Deployment order

Perform the binding order without parallelising steps:

1. run the reviewed Alembic migration task and verify the expected head;
2. run the control task definition once with command override
   `python -m orchestration.bootstrap`; require exit zero;
3. deploy `ledger` and confirm exactly one running task, queue polling and no
   advisory-lock contention;
4. deploy `control` and confirm two running tasks and Schedule visibility;
5. deploy `docintel` and confirm two running tasks;
6. deploy `effects` and confirm one running task;
7. deploy the API image without introducing a request-path Temporal client;
8. run claim-read availability smoke, Worker-poll smoke, a second idempotent
   bootstrap, Schedule-definition comparison and CloudWatch alarm checks.

Do not deploy production until the T10 report is complete and the owner makes
the separate go-live decision.

## Healthy signals

- ECS desired/running counts are `control=2`, `docintel=2`, `effects=1`,
  `ledger=1`.
- SDK metrics arrive in `Pacha/TemporalSDK` with environment, build and role.
- Derived operational metrics arrive in `Pacha/Temporal`; missing data is
  alarmed rather than treated as healthy.
- The oldest outbox delivery remains below 5 minutes and the oldest unledgered
  event below 60 seconds.
- Control schedule-to-start p95 remains at or below 30 seconds and a control
  Worker polls at least once every 2 minutes.
- No Codec/KMS, ledger-chain, uncertain-write or Schedule-action counter is
  non-zero; the non-blocked Workflow failure rate remains at or below 1% over
  15 minutes.
- Old builds remain deployed while Temporal reports pinned open Workflows.

Logs may contain only `workflow_id`, `workflow_run_id`, `workflow_type`,
`activity_type`, `run_ref`, `step_id`, `attempt`, `task_queue`, `build_id`,
`status/error_code` and `duration_ms`. Treat any claim fact or PII in a Worker
log as a security incident.

## Temporal outage

1. Confirm claim list/detail and ordinary PostgreSQL writes still work. If they
   do not, this is not only a Temporal outage; follow the database/API incident
   path.
2. Stop deployment churn. Do not replay, mutate histories or clear outbox rows.
3. Check Temporal status, network egress, DNS, mTLS secret access and namespace
   identity using references only—never print secret material.
4. Observe `events`/`event_deliveries` and ledger backlog in PostgreSQL. Work is
   retained there and should resume when polling returns.
5. Restore connectivity, confirm control polling, then allow finite drains to
   process at most 500 rows per execution. Escalate if backlog grows across
   consecutive executions.

## Stuck Workflow

1. Identify it by Workflow id/run id and correlate to `agent_runs`; never use a
   Temporal Query as claim truth.
2. Inspect Workflow type, current Activity/timer, retry count, build pin and
   sanitised error code. Read domain state from PostgreSQL.
3. If it awaits review/input, resolve through the normal review/domain API so a
   committed event sends the opaque Signal. Never signal a made-up resolution.
4. If an idempotent Activity failed transiently, correct the dependency and let
   its bounded policy retry. Do not manually repeat an external write.
5. Retain the pinned Worker build until no open execution uses it. Replay the
   captured history before any Workflow-code remediation.

## Uncertain external write

`uncertain_write` means the provider may have accepted the write but Pacha did
not obtain a conclusive acknowledgement/readback.

1. Do not retry the Activity, restart the Workflow to repeat it, or mark it
   successful from Temporal state.
2. Preserve the stable write id and provider evidence.
3. Use the governed provider readback/probe path. If it cannot prove outcome,
   leave the `EXCEPTION{uncertain_write}` for human reconciliation.
4. Record the operator decision through its versioned review contract. Never
   edit the projection/outbox row directly.

## Codec/KMS failure

1. Stop the affected rollout; there is no plaintext or API-key fallback.
2. Check task-role permission for only the configured key ARN, KMS health,
   Secrets access and whether the immutable key ARN matches history metadata.
3. Never retarget an alias or replace the configured ARN to make decoding pass.
   Restore access to the same key; its retained rotated material decrypts old
   data keys.
4. Never print ciphertext metadata, wrapped data keys, PEM bytes or provider
   exception text. Use the sanitised failure counter and AWS request ids in the
   restricted incident record.
5. After remediation, prove encode/decode against a non-PII control payload and
   replay committed histories before resuming rollout.

## Rollback

Roll back only the affected Worker role to the previous immutable image/build.
For `ledger`, ECS uses a stop-then-start deployment so two tasks do not overlap.
For other roles, retain both deployments while pinned executions exist and
route new executions according to Temporal Worker Deployment versioning.

Never roll back the database without a separately reviewed migration plan.
After rollback, verify running counts, queue polling, pinned builds, Schedule
definitions, outbox/ledger drain and claim-read availability.
