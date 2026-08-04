---
id: TEMPORAL-T09
prd_ref: docs/architecture/TEMPORAL_IMPLEMENTATION_MASTER_PLAN.md §§20–25
title: ECS/Fargate Worker deployment, IAM, secrets, KMS and observability
depends_on: [TEMPORAL-T08]
branch: codex/temporal-t09-cloud-infrastructure
blast_radius: true
acceptance_tests:
  - tests/unit/test_temporal_t09.py
  - tests/integration/test_temporal_t09.py
status: ready_for_review
pr: null
attempts: 0
reason: null
---

# TEMPORAL-T09 — Cloud infrastructure

## 1. What is built

Terraform modules and staging/production roots create the four isolated Worker
services, strict IAM and egress, logs, SDK telemetry, operational alarms and an
immutable one-shot bootstrap path. `python -m orchestration.runtime` owns the
closed registrations for each role; `python -m orchestration.bootstrap` creates
missing Schedule definitions and refuses drift.

## 2. Safety boundaries

- Images and collector images are digest-pinned; the application build id is a
  full git SHA and labels every task definition, stream and Worker Deployment.
- Workers have no listener, load balancer, public IP or ingress rule.
- Temporal/provider egress accepts explicit CIDRs only and refuses
  `0.0.0.0/0`; AWS and PostgreSQL egress uses security-group references.
- mTLS material remains in Secrets Manager and is fetched into process memory
  by the existing client. Terraform never places PEM bytes in state or ECS
  environment values.
- The application dependency factory may return domain dependencies only.
  Workflow and Activity registrations are code-owned, exact and role-closed.
- The ledger service remains one task, one Activity at a time, with the binding
  PostgreSQL advisory lock still enforcing the true single-writer boundary.

## 3. Explicit non-goals

T09 does not invent a Temporal Cloud namespace/region pairing, credentials,
provider endpoints, application adapter implementations, staging observations
or recovery evidence. The unresolved deployment inputs are registered under
ED-11; the Terraform roots have required slots and fail before apply when they
are absent. T10 owns the real Cloud/RDS failure trial.

## 4. Acceptance

Terraform formatting/schema validation and the T09 suites verify the exact
topology, IAM verbs, fail-closed egress, immutable deployment identity, alarm
thresholds, telemetry boundary, explicit factory and role-owned registration.
Full repository checks remain required before merge.
