## Section 0 — Shared Engineering Decisions (binding on all PRDs)

> **v1.1, architecture freeze 2026-07-24** — incorporates the durable
> orchestration and build-vs-buy decision in ADR-001–004. Where any other
> document conflicts with this file, **this file wins**; raise the conflict on
> the open-items register (see AGENT_BUILD_GUIDE.md §5) — do not resolve it
> locally.

**ED-1 · Topology.** Modular monolith, single deployable, single repo (monorepo: `/platform`, `/agents`, `/packs`, `/console`, `/infra`). Do not build microservices; team size and claim volume (Mayfair motor = 823 claims YTD ≈ 3–4/day; design envelope **50 claims/day** = all LOBs + 10× growth) do not justify them. Module boundaries are enforced in code (each PRD = one Python package with a public interface) so extraction to services later is possible, never required now.

**ED-2 · Stack and durable orchestration.** Backend: Python 3.12, FastAPI,
Pydantic v2, SQLAlchemy 2 + Alembic migrations. **Temporal Cloud is the selected
permanent durable workflow engine; the CTO/owner approved implementation on
2026-07-24 after technical, privacy, operations and procurement review. Do not
self-host Temporal.** Implement T01→T10 exactly as specified in
`architecture/TEMPORAL_IMPLEMENTATION_MASTER_PLAN.md`. Pacha is not live:
replace the existing Celery 5/Redis 7/Beat orchestration in the codebase and
remove those runtime dependencies before launch; do not build a dual-runtime
selector or permanent compatibility layer. T09/T10 Cloud/RDS evidence remains
the go-live gate. AWS Step Functions remains the reviewed fallback only if the
staging failure trial invalidates Temporal. Frontend: React 18 + TypeScript +
Vite, TanStack Query, Tailwind, pdf.js (citation viewer). IDs: ULIDs everywhere
(sortable, no coordination). All timestamps UTC in storage, rendered EAT
(UTC+3) in UI.

**ED-3 · Managed infrastructure.** AWS **af-south-1** for Pacha compute and
data (Cape Town — document cross-border transfers under Kenya DPA 2019 §48 in
the DPIA). Production uses RDS PostgreSQL 16 with PITR, S3 with SSE-KMS and
Object Lock for immutable artifacts, AWS KMS and Secrets Manager, ECS/Fargate
or another approved managed compute service, CloudWatch + OpenTelemetry +
Sentry, and GuardDuty Malware Protection for S3 uploads. Uploads remain
quarantined until a managed malware scan reports them safe. ElastiCache Redis
is not part of the target architecture and is removed in T08 after Temporal
replacements pass. Temporal Cloud region is separately gated: the exact
available region and cross-border posture must pass latency measurement and the
DPIA before use. Terraform for Pacha-owned infrastructure; zero click-ops.
Environments: `dev`, `staging`, `prod`. **Production PII never leaves prod** —
dev/staging run on the synthetic + anonymised corpus only.

**ED-3a · External UI execution.** Paste-assist is the safe production mode.
Pacha does not build a Playwright runner, runner leasing/hosting, session
management or generic browser automation. A commodity UI executor is purchased:
evaluate Microsoft Power Automate Desktop first and UiPath only if Power
Automate cannot meet the required controls. Pacha prepares the exact payload,
applies autonomy/authority, creates the stable idempotency key, requires
execution evidence, independently reads the target back and reconciles it.
Vendor AI selector repair/self-healing is disabled; selector miss or unexpected
UI state fails closed as `EXCEPTION{ui_drift}`. An uncertain external write is
`EXCEPTION{uncertain_write}` and is never blindly retried. Vendor, target
reachability, data-processing, service identity and commercial approval remain
open gates; the first pilot stays paste-assist until they pass.

**ED-4 · LLM access.** Anthropic API, two tiers referenced throughout as `MODEL_HEAVY` (Sonnet-class: extraction, generation, vision) and `MODEL_LIGHT` (Haiku-class: classification, relevance, verification). Model IDs live in config, never in code — swapping models must be a config change. All calls: structured output via tool-use JSON schemas, `temperature=0` for extraction/rules paths, request/response logged (with PII field-level redaction rules from ED-6) to the audit ledger. Zero-data-retention arrangement with the provider documented in the DPIA.

**ED-4a · Launch model config & failure taxonomy.** Launch config values: `MODEL_HEAVY = claude-sonnet-4-6`, `MODEL_LIGHT = claude-haiku-4-5-20251001`. Each tier carries a `fallback_model_id` (the previous pinned version of the same tier). Failure handling, binding on the AR-4 wrapper:
- **Transport errors / HTTP 429 / 5xx / timeout →** silent bounded retry: exponential backoff 1s → 60s, max 6 attempts, ≤ 10 min total; switch to `fallback_model_id` after attempt 3.
- **Schema-invalid structured output →** exactly one regeneration attempt, then `EXCEPTION` review item.
- **Budget breach (AR-4 table) →** `EXCEPTION{type: budget_exceeded}` immediately, no retry.
- **Provider fully down (retries exhausted) →** agent run pauses; Temporal
  records the non-sensitive control outcome and resumes from a configured
  retry/Signal after recovery. T08 removes the superseded reaper.

**ED-5 · Email integration.** Microsoft Graph API against a **shared claims
mailbox** (this is a launch precondition — resolves ODQ-1; Aryia to get
`claims@mayfair` provisioned). App registration with application permissions
`Mail.Read`, `Mail.Send`, scoped by an Exchange Application Access Policy to
that mailbox only. T07 installs a 71-hour Temporal renewal Schedule for the
change-notification webhook and an authoritative delta-query poll every 60
seconds. Outbound mail always sends from the shared mailbox with the officer
visible in signature per template.

**ED-6 · Security baseline.** Staff SSO remains Microsoft Entra ID (OIDC) —
Mayfair is an Outlook shop, so users exist already. Do not add Auth0 or build a
staff identity system. RBAC roles, financial authority bands and review
permissions remain Pacha domain logic per PRD-04. If an external identity
surface is ever approved later, use a managed service such as Entra External ID;
do not build magic-link tokens or session management. PII fields (national ID,
KRA PIN, DL number, phone, bank details) are envelope-encrypted and
access-logged, mechanics per ED-6a.

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
- Vendor-executor evidence screenshots/artifacts: S3 lifecycle → Glacier Instant
  Retrieval at 90 days; deleted with the claim at 7 years.
- PRD-01 page renders (PNG): **deleted at 180 days, regenerated on demand** (fully derivable from the immutable original).
- SLA clock rows, chase items, savings ledger, audit ledger: **never purged** (stated in their PRDs; repeated here as the retention source of truth).

**ED-10 · Pack integrity.** v1 pack signing = sha256 + `pack_registry` row; the loader verifies the sha at boot and refuses mismatches. No asymmetric signing until packs cross an organisational boundary (KMS upgrade path noted in PRD-13, deferred).

**ED-11 · Ambiguity protocol (binding on coding agents).** If, while implementing, anything is underdetermined — a missing value, a conflict between documents, an unstated behaviour — **do not decide locally.** Implement the narrowest safe behaviour (`blocked_on_inputs`, `EXCEPTION`, or refuse-to-render, matching the platform's never-guess doctrine), and add an entry to the open-items register (Phase 3 document) describing the gap. The register, not the codebase, is where ambiguity goes to die.
