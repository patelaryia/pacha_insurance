# ADR-002 — Pacha build-versus-buy boundaries

- **Status:** accepted
- **Decision date:** 2026-07-24
- **Owners:** Pacha CTO and repository owner

## Context

Pacha's differentiation is reliable insurance-claims reasoning and control, not
hosting commodity workflow, identity, browser-automation, auction, payment,
signature or security infrastructure.

## Decision

Pacha builds and owns:

- canonical claim state, append-only field versions and event history;
- evidence/citation provenance and document interpretation;
- insurance rules, calculations and LOB packs;
- human review, financial authority and autonomy controls;
- evaluation, promotion and demotion machinery;
- approval-pack, repair and settlement workflow logic;
- exact external payload preparation, stable idempotency keys and authority
  checks;
- target-system readback, reconciliation and zero-silent-divergence controls;
- operational, cycle-time, recovery and savings measurement.

Pacha buys or uses managed services for:

- durable workflow control, subject to ADR-001's gate;
- RPA/UI execution, subject to ADR-003;
- auction execution and bidder operations, subject to ADR-004;
- staff identity through Microsoft Entra ID;
- any future external identity through a managed external identity service,
  such as Entra External ID;
- RDS PostgreSQL with PITR, S3 SSE-KMS and Object Lock, AWS KMS and Secrets
  Manager, approved managed compute, CloudWatch, OpenTelemetry and Sentry;
- managed upload malware scanning through GuardDuty Malware Protection for S3;
- electronic signatures, if later required, through an established provider.

Pacha does not build custom database hosting, key or secret infrastructure,
malware scanners, monitoring/paging platforms, staff authentication, external
identity tokens/sessions, payment rails or electronic-signature infrastructure.

Uploaded documents remain quarantined and unavailable to the document pipeline
until a managed malware scan returns a safe result.

## Payments and authority

Pacha never executes payments or holds funds. It may prepare, validate, project
and reconcile settlement instructions. Mayfair's finance, approval and banking
systems move money. No new payment service provider is introduced for the first
pilot.

Claims roles, authority bands, review permissions and committee decisions are
Pacha domain logic even when identity, workflow or execution infrastructure is
managed elsewhere.

## Consequences

Every new packet must identify whether it changes differentiated domain logic
or a commodity boundary. Commodity runtime work requires an explicit exception
to this ADR and owner approval.
