## Section 0.5 — Shared Agent Runtime (binding on PRD-05–12)

**AR-1 · Agent run model.** Every agent execution is a durable record, never an ephemeral function call:

sql

```sql
CREATE TABLE agent_runs (
  id             TEXT PRIMARY KEY,           -- ULID = correlation_id for all child events
  agent          TEXT NOT NULL,              -- 'intake'|'chase'|'assessment'|'pack'|'projection'
  capability_id  TEXT NOT NULL,              -- FK capabilities (PRD-03)
  claim_id       TEXT,
  trigger_event  TEXT REFERENCES events(id),
  status         TEXT NOT NULL,              -- 'running'|'awaiting_review'|'completed'|'failed'|'blocked'
  steps          JSONB NOT NULL DEFAULT '[]',-- [{step_id, started, ended, outcome, refs}]
  autonomy_level TEXT NOT NULL,              -- snapshot of capability level at run start
  error          JSONB,
  started_at TIMESTAMPTZ NOT NULL, ended_at TIMESTAMPTZ
);
```

Each agent's legal step sequence is declared as a **COP step definition** in the pack (`{capability_id, steps: [{id, expects_events[], produces[]}]}`) — this is what grader `G-PROC` checks conformance against. Steps are idempotent Celery tasks; a crashed run resumes from its last completed step (state in `steps`, not in memory). A run that emits a review item moves to `awaiting_review` and **ends its turn**; resolution events resume it. No agent ever blocks a worker waiting on a human.

**AR-1a · Run recovery (belt and braces).** Celery configured `acks_late=True`. In addition, a reaper Beat job (every 5 min) re-enqueues the current step of any run with `status='running'` whose step heartbeat (`steps[].updated_at`) is > 15 minutes stale. Steps are idempotent by the AR-1 contract, so re-enqueue is always safe. Max **3 resume attempts per step**, then run `status='failed'` + ops alert.

**AR-2 · Autonomy gate (single choke point).** Every side-effectful action goes through one function: `execute_or_stage(capability_id, action, claim_id) →` at L0 log only; L1 create `DRAFT_RELEASE` review item; L2 create typed confirm item; L3 execute + maybe-sample into review; L4 execute. Critical grader failure on the action's payload forces the L1 path regardless of level (PRD-03 §3.3 gating rule). Agents contain **zero** direct sends/writes outside this gate — CI greps for banned direct calls (`graph_client.send`, adapter `.execute`) outside the gate module.

**AR-3 · Outbound communications service.** One service sends all email: `send(template_id, claim_id, to_party_ids, attachments) →` renders via PRD-02 (StrictUndefined, verification floors), runs `G-COMM` (recipient ∈ claim parties; no unverified money figures; template registered), passes the autonomy gate, sends via Graph from the shared mailbox, writes `communications` + ledger + `email.sent`. Attachment rule: only S3 artifacts already on the claim. Reply-To always the shared mailbox so responses re-enter PRD-05 routing.

**Scope (explicit):** AR-2/AR-3 govern **claim-party communications only**. Internal staff notifications go through the `notify` module (AR-5) and are exempt from G-COMM and the autonomy gate.

**AR-3a · Send window.** All non-urgent outbound (chase reminders, doc requests, and anything not SLA-critical) passes through a send window: **08:00–18:00 EAT, Mon–Sat, Kenya public holidays excluded** (holiday calendar ships in the pack, `sla/holidays.yaml`, maintained annually). Sends falling due outside the window queue to the next window open. Acknowledgements (`intake.acknowledge`) are exempt — they send 24/7 (the trial metric counts round-the-clock).

**AR-3b · Graph mechanics.** Outbound attachments > 3MB: Graph upload sessions, 4MB chunks. Inbound message cap 25MB; larger → `EXCEPTION` with manual handling. Send throttle: 30 messages/min platform-wide token bucket; queue beyond. Webhooks: per-subscription random `clientState`, validated on every notification. **The 60s delta-query poll is authoritative**; the webhook is a latency optimisation only, so the 71h subscription-renewal gap costs at most one poll interval.

**AR-5 · Internal notify module.** A separate `notify/` module sends all staff notifications (PRD-04 §4.4): direct Graph send is permitted here, recipients restricted to an allowlisted staff domain, templates registered, **exempt from G-COMM and the autonomy gate**, every send still ledgered. The AR-2 CI banned-call grep whitelists the `notify/` path.

**AR-4 · LLM call wrapper.** `llm(purpose, model_tier, schema, inputs)` — enforces temperature 0 for extraction/decision purposes, tool-use structured output, token/cost budgets per the table below (breach → `EXCEPTION{type: budget_exceeded}`, never a silent retry), redacted logging per ED-6, and prompt template versioning (`prompt_templates` table: id, version, body, purpose — prompts are data, referenced by graders and eval runs). Transport/format failures follow the ED-4a taxonomy.

**AR-4a · Budget table (config, not code — these are launch values):**

| purpose | tier | max in / out tokens | max $/call | extra cap |
|---|---|---|---|---|
| mailbox triage | LIGHT | 8k / 300 | 0.02 | — |
| doc/page classify | LIGHT | 6k / 200 | 0.02 | — |
| extraction (per doc) | HEAVY | 40k / 4k | 0.45 | $0.60/doc p95 incl. retries (PRD-01) |
| CC-5 consistency | HEAVY | 25k / 1k | 0.30 | 1 per claim per assessor report |
| note commentary | HEAVY | 15k / 2k | 0.25 | — |
| G-NOTE grade | HEAVY | 12k / 1k | 0.18 | — |
| G-CITE verify (per field) | LIGHT | 1.5k / 100 | 0.01 | 40 fields/doc |
| Path-B shadow (PRD-07) | HEAVY | 20k / 500 | 0.25 | **$10/day platform-wide** |
| Swahili gloss | HEAVY | 8k / 1k | 0.10 | — |

Per-claim ceilings: **$8/day, $12 lifetime** → `EXCEPTION{type: budget_exceeded}` pauses that claim's agent runs; a human unblocks. Per-call token breach: input is truncated per a documented priority order (newest documents first, communications last) — never a silent failure.