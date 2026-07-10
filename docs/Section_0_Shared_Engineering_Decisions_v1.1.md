## Section 0 — Shared Engineering Decisions (binding on all PRDs)

> **v1.1** — incorporates the CTO decision round of 2026-07. ED-8 through ED-11 are new. ED-3, ED-4 and ED-6 are amended. Where any other document conflicts with this file, **this file wins**; raise the conflict on the open-items register (see AGENT_BUILD_GUIDE.md §5) — do not resolve it locally.

**ED-1 · Topology.** Modular monolith, single deployable, single repo (monorepo: `/platform`, `/agents`, `/packs`, `/console`, `/infra`). Do not build microservices; team size and claim volume (Mayfair motor = 823 claims YTD ≈ 3–4/day; design envelope **50 claims/day** = all LOBs + 10× growth) do not justify them. Module boundaries are enforced in code (each PRD = one Python package with a public interface) so extraction to services later is possible, never required now.

**ED-2 · Stack.** Backend: Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 + Alembic migrations. Async jobs: Celery 5 on Redis 7; Celery Beat for scheduled ticks. Frontend: React 18 + TypeScript + Vite, TanStack Query, Tailwind, pdf.js (citation viewer). IDs: ULIDs everywhere (sortable, no coordination). All timestamps UTC in storage, rendered EAT (UTC+3) in UI.

**ED-3 · Infrastructure.** AWS **af-south-1** (Cape Town — lowest-latency region with a full service set; document the cross-border transfer under Kenya DPA 2019 §48 with safeguards in the DPIA). RDS PostgreSQL 16 (PITR enabled), ElastiCache Redis, S3 with SSE-KMS for all documents/artifacts, ECS Fargate (2 services: `api`, `worker`), Secrets Manager, CloudWatch + OpenTelemetry traces + Sentry. Terraform for all infra; zero click-ops. Environments: `dev`, `staging`, `prod`. **Production PII never leaves prod** — dev/staging run on the synthetic + anonymised corpus only.

**ED-3a · RPA runner topology (resolves DG-3 pessimistically — build this regardless of the DG-3 answer).** The Playwright RPA worker is packaged as a standalone container ("runner") that makes **outbound-only HTTPS** connections: it pulls jobs from the platform queue API, pushes evidence screenshots to S3, and heartbeats. No inbound ports, no VPN dependency, no assumption that ICON/EDMS are internet-reachable from AWS. Deployment target is decided by DG-3 (open item 12): if ICON/EDMS are LAN-only, the runner ships on a Mayfair-provided VM/mini-PC on their network; if internet-reachable with IP allowlisting, the identical container runs on Fargate behind a NAT gateway with static EIPs and the on-prem host is never provisioned. Nothing else in the architecture changes between the two cases. Paste-assist is unaffected either way (it runs in the officer's browser). VPC/Terraform proceeds now on this design.

**ED-4 · LLM access.** Anthropic API, two tiers referenced throughout as `MODEL_HEAVY` (Sonnet-class: extraction, generation, vision) and `MODEL_LIGHT` (Haiku-class: classification, relevance, verification). Model IDs live in config, never in code — swapping models must be a config change. All calls: structured output via tool-use JSON schemas, `temperature=0` for extraction/rules paths, request/response logged (with PII field-level redaction rules from ED-6) to the audit ledger. Zero-data-retention arrangement with the provider documented in the DPIA.

**ED-4a · Launch model config & failure taxonomy.** Launch config values: `MODEL_HEAVY = claude-sonnet-4-6`, `MODEL_LIGHT = claude-haiku-4-5-20251001`. Each tier carries a `fallback_model_id` (the previous pinned version of the same tier). Failure handling, binding on the AR-4 wrapper:
- **Transport errors / HTTP 429 / 5xx / timeout →** silent bounded retry: exponential backoff 1s → 60s, max 6 attempts, ≤ 10 min total; switch to `fallback_model_id` after attempt 3.
- **Schema-invalid structured output →** exactly one regeneration attempt, then `EXCEPTION` review item.
- **Budget breach (AR-4 table) →** `EXCEPTION{type: budget_exceeded}` immediately, no retry.
- **Provider fully down (retries exhausted) →** agent run pauses; the reaper (AR-1a) resumes it.

**ED-5 · Email integration.** Microsoft Graph API against a **shared claims mailbox** (this is a launch precondition — resolves ODQ-1; Aryia to get `claims@mayfair` provisioned). App registration with application permissions `Mail.Read`, `Mail.Send`, scoped by an Exchange Application Access Policy to that mailbox only. Change-notification webhook (renewed every 71h by a Beat job) + delta-query poll every 60s as fallback. Outbound mail always sent from the shared mailbox with the officer visible in signature per template.

**ED-6 · Security baseline.** SSO via Microsoft Entra ID (OIDC) — Mayfair is an Outlook shop, so users exist already. RBAC roles defined in PRD-04. PII fields (national ID, KRA PIN, DL number, phone, bank details) are envelope-encrypted and access-logged, mechanics per ED-6a.

**ED-6a · PII encryption mechanics (binding implementation of the ED-6 requirement against the `claim_fields` model).**
- When `claim_fields.pii_class != 'none'`, `value` is stored as an envelope-encrypted blob (AES-256-GCM).
- **DEK per claim**, wrapped by the KMS CMK, stored in `claims.dek_wrapped`. The 7-year crypto-shred is therefore genuinely per-claim: retention expiry deletes the wrapped DEK.
- Equality-search paths (dedupe, inbound matching) use a `value_search` blind-index column on `claim_fields` = HMAC-SHA256 of the normalised value under a dedicated KMS-held index key. Populated for exactly: national ID, KRA PIN, DL number, phone, bank account number.
- **Registration plates stay plaintext.** They are the platform's universal join key (thread matching, dedupe, EDMS naming); classified `personal-low`, justified in the DPIA.
- Decrypt permissions: extraction workers and the citation-viewer API hold KMS grants; every decrypt is access-logged with user id + field path. TLS 1.2+ everywhere. Audit ledger is hash-chained (PRD-00 FR-6). Retention: claim records 7 years post-closure (insurance statutory posture), then crypto-shred via key deletion. Before prod go-live: ODPC data-processor registration + DPIA (Aryia owns; blocks nothing in build, blocks launch).

**ED-7 · Definition of done (every PRD).** Unit tests ≥ 80% on engine code, integration test per acceptance scenario, Alembic migration reviewed, OpenAPI spec generated, runbook page, grader coverage registered in PRD-03, demo on staging with the synthetic claim set.

**ED-7a · Coverage boundary & CI gates (defines "engine code").** Tool: pytest-cov. **In scope ≥ 80%:** `platform/*`, `agents/*`. **In scope 100% (separate CI rule):** `packs/*/calcs.py`. **Out of scope:** `infra/`, Alembic migrations. Frontend: vitest ≥ 70%. Grader-coverage gate: the build emits `grader_map.yaml` (OutputType → grader ids); a CI test asserts every OutputType enum member maps to ≥ 1 grader with `severity: critical`.

**ED-8 · Money (binding everywhere — extraction, calcs, storage, APIs, fixtures, tests).** `Money = BIGINT, KES cents`, end to end. Literal convention in all specs and code: cents written with the `_00` suffix style (e.g. `15_000_00` = KES 15,000). The `money_kes` validator (PRD-01 §1.3) parses shilling-denominated strings from documents and **multiplies by 100 on commit**; explicit cents in source documents (rare) are parsed when present. Display rule: render as shillings; show cents only when nonzero. **Never floats for money anywhere in the platform** — CI lint rule bans `float` in any signature typed `Money`. Normalisations already applied in v1.1 documents: R-12 threshold = `4_000_000_00`; all rule/routing literals are cents.

**ED-9 · Retention & partitioning.**
- `events`: monthly partitions (pg_partman), retained 7 years.
- `grader_runs`, `agent_runs`: monthly partitions; 3 years full, aggregated statistics only thereafter.
- LLM request/response logs: 1 year full, metadata-only thereafter.
- RPA evidence screenshots: S3 lifecycle → Glacier Instant Retrieval at 90 days; deleted with the claim at 7 years.
- PRD-01 page renders (PNG): **deleted at 180 days, regenerated on demand** (fully derivable from the immutable original).
- SLA clock rows, chase items, savings ledger, audit ledger: **never purged** (stated in their PRDs; repeated here as the retention source of truth).

**ED-10 · Pack integrity.** v1 pack signing = sha256 + `pack_registry` row; the loader verifies the sha at boot and refuses mismatches. No asymmetric signing until packs cross an organisational boundary (KMS upgrade path noted in PRD-13, deferred).

**ED-11 · Ambiguity protocol (binding on coding agents).** If, while implementing, anything is underdetermined — a missing value, a conflict between documents, an unstated behaviour — **do not decide locally.** Implement the narrowest safe behaviour (`blocked_on_inputs`, `EXCEPTION`, or refuse-to-render, matching the platform's never-guess doctrine), and add an entry to the open-items register (Phase 3 document) describing the gap. The register, not the codebase, is where ambiguity goes to die.