# PACKET-13 — Agent runtime (AR-1/AR-2/AR-3) & intake router/assigner (Phase-2 kickoff; PRD-05 slice 1 of 2)

> **Status:** issued · **Builder:** Codex per `AGENTS.md` · **Reviewer:** CTO per `CLAUDE.md`
> **Source spec:** `docs/Section_0.5_Shared_Agent_Runtime_v1.1.md` AR-1/AR-1a/
> AR-2/AR-3/AR-3a/AR-4; `docs/PRD-05_Intake_and_Triage_Agent_v1.1.md`
> §5.2 (email router, final classifier contract), §5.6 (capabilities), §5.7
> scenarios 2 and 5, §5.8 (assignment); PRD-03 §3.3 (gating); PRD-04 §4.3
> (closed enum, REOPEN_PROMPT); Section 0 ED-8/ED-11; guide §3.11 ceilings;
> registers #24/#50/#61/#67/#68/#71/#78.
> Precedence: Section 0 → Section 0.5 → PRD-05/PRD-03/PRD-04/PRD-00 →
> PACKET-06..12 contracts → this packet.
> **Depends on:** PACKET-12 merged on main (including registers #108–#118).
> **Acceptance:** `tests/acceptance/test_packet_13_agent_runtime.py` —
> protected, failing by design until this packet is built.
> **Packet 14 (next):** PRD-05 slice 2 — intake flow S1–S8 as a real
> `agent_runs` COP run, dupe/late checks, triage Mode A coverage card,
> decline path + T-07, scenarios 1/3/4/6, KPI wire.

## 0. Slice boundary

Phase 2 begins. This packet builds the shared agent substrate every agent PRD
needs — the durable run record (AR-1), the single side-effect choke point
(AR-2, closing register #68), and the outbound-communications service (AR-3)
— plus the deterministic half of PRD-05: the §5.2 email router with its final
five-class contract, and §5.8 auto-assignment. The S1–S8 intake flow itself is
PACKET-14; this slice hands `new_intimation` mail to it via a durable
`intake.requested` event (proposed #121).

**No live Graph exists** (open item 1): `email.received` has no producer yet
(tests emit durable synthetic events — proposed #120), and the AR-3 transport
slot is `pending_capture` — an execute-path send terminates in a visible
blocked state, never a fake `email.sent`.

## 1. Scope

**In:**

1. **`platform/agent_runtime/` (import `agent_runtime`)** — shared runtime,
   not an agent (naming per proposed #119):
   - **`agent_runs` table — AR-1 DDL verbatim** (id, agent, capability_id,
     claim_id, trigger_event, status
     `'running'|'awaiting_review'|'completed'|'failed'|'blocked'`, steps JSON,
     autonomy_level, error, started_at, ended_at) on `claim_core` Base;
     migration `0010_agent_runs`. The **only** new table in this packet.
   - **Runner (AR-1/AR-1a):** step sequences declared in pack
     `packs/motor/cop_steps.yaml`; steps are idempotent callables; a crashed
     run resumes from its last completed step (state in `steps`, never
     memory); a run that emits a review item moves to `awaiting_review` and
     ends its turn — `review.resolved` resumes it; per-step heartbeat
     (`steps[].updated_at`); reaper Beat task (5 min) re-enqueues `running`
     runs with heartbeat >15 min stale, max 3 attempts per step, then
     `status='failed'` + `EXCEPTION{agent_run_failed}` review item (the AR-1a
     "ops alert" until item 1 supplies a real alert channel — proposed #125).
     No agent blocks a worker waiting on a human.
   - **`execute_or_stage` gate (AR-2, register #68):** the single choke
     point. Level semantics exactly: L0 log-only (run/event record, nothing
     staged, nothing executed); L1 → `DRAFT_RELEASE` review item; L2 → typed
     confirm item, the type read from pack
     `packs/motor/agent_runtime/gate.yaml` (`confirm_types:
     capability_id → review type`, closed-enum members only) — an unmapped
     capability at L2 **fails closed to the L1 draft path** (proposed #126);
     L3 execute + sample into `SAMPLE_REVIEW` per the capability policy's
     `sampling_rate`; L4 execute. A blocked grading decision (critical fail,
     PRD-03 §3.3 via the PACKET-08 `grade_output` seam) **forces the L1 path
     at any level**. Executors are registered callables;
     **no funds-transfer action type is registrable or executable** — the
     forbidden set (`settlement.*` transfer ops, payment vouchers as
     execution, anything moving money) is hard-coded constitution
     (guide invariant 8; PRD-12 acc. 6) and registration raises `ValueError`.
   - **AR-3 `send(template_id, claim_id, to_party_ids, attachments)`:**
     renders via the PACKET-07 template engine (StrictUndefined,
     `min_verification` floors; a `pending_capture` template body yields a
     **visible** pending-template draft state, never invented prose — #61
     pattern); runs **G-COMM as implemented deterministic checks** (recipient
     party ids ⊆ that claim's `parties`; no under-verified merge field — the
     render floor enforces it; template registered) — this replaces the
     PACKET-08 `PendingGrader` registration for G-COMM and closes that arm of
     #67 (proposed #129); a G-COMM failure **refuses** the send with a
     visible `EXCEPTION` review item; then passes `execute_or_stage`
     (capability-level staging; launch levels make every send a draft);
     **send window AR-3a**: 08:00–18:00 EAT Mon–Sat excl. `sla/holidays.yaml`
     — an out-of-window non-exempt send returns a visible `queued_window`
     outcome; `intake.acknowledge` is window-exempt via
     `gate.yaml exempt_capabilities` (24/7 per AR-3a). Durable window-queue
     release machinery lands with the item-1 Graph transport packet
     (proposed #130). Transport: Graph slot `pending_capture` — the execute
     path (L3/L4) stops at a visible transport-blocked state; `email.sent`
     and outbound `communications` rows are written only by a real send,
     which cannot happen in this packet.
2. **`agents/intake_agent/` — PRD-05 slice 1:**
   - **Email router (§5.2, strict order, verbatim):** consumer on
     `email.received` events (payload pinned in §3.1). (1) thread match on
     `conversation_id` ∈ `communications.thread_id` → attach to that claim +
     `document.received` per attachment; (2) reference match — our claim id
     or reg plate (existing `kenya_reg` validator) in subject/body matching
     **exactly one** open claim → attach + `INBOUND_ATTACHED` timeline event;
     (3) >1 open-claim match → `EXCEPTION{subtype: ambiguous_inbound}` with
     the candidate list — never guess; (4) classifier (seam-injectable;
     production implementation = AR-4 `MODEL_LIGHT` call through
     `doc_intel.llm.ModelWrapper` with the prompt as pack data, #81 pattern)
     routed by the **final five-class table verbatim**, boundaries inclusive:
     `new_intimation ≥ 0.85` → durable `intake.requested` event (S1–S8 land
     in PACKET-14 — proposed #121); `new_intimation < 0.85`, 
     `claim_related_unmatched`, `not_a_claim < 0.95`, `unclear` →
     `DOC_CLASSIFY{subtype: mailbox_triage}`; `multi_intimation` (any) →
     `EXCEPTION{subtype: multi_claim_email}`; `not_a_claim ≥ 0.95` →
     `mail.archived` event + queryable inbound row + deterministic 10%
     `SAMPLE_REVIEW` (hash of `graph_message_id`, rate is pack config — no
     RNG; proposed #127). The Graph archive-label write itself is blocked on
     item 1. **Mail is never silently dropped.**
   - **Idempotency:** `communications.graph_message_id` unique; a redelivered
     message is a no-op. **Loop guard:** `from_addr` ∈ configured
     `self_addresses` → skip entirely, hard-coded check (§5.2 permits);
     addresses are org config captured with item 1 (proposed #120).
   - **Terminal-state inbound (§5.2 v1.1):** always attach + timeline note +
     owner-notification event — **never a state change**. DECLINED/WITHDRAWN
     + attachment-bearing inbound → `REOPEN_PROMPT` review item ("substantive
     new evidence" narrowed to has-attachments, capture confirmation wanted —
     proposed #122); SETTLED/CLOSED money-relevant `EXCEPTION` needs the
     doc-type signal and lands in PACKET-14 (same register).
   - **Assigner (§5.8):** consumer on `claim.created` — weighted round-robin,
     weight = open-claim count per officer (fewest wins; ties break by
     lexicographic actor id — deterministic), writes `claims.assigned_to`,
     emits `claim.assigned` (ledgered via ACTION_MAP addition — proposed
     #128). Officer pool = `roles.yaml` `claims_officer` actors, injectable
     for tests. Reassignment UI stays PRD-04 console scope.
3. **Capability reconciliation (§5.6 is the authoritative table — proposed
   #123).** `packs/motor/autonomy/policies.yaml`: add missing
   `intake.doc_request` (max L4); `triage.decline_draft` max **L2** (release
   still human via DRAFT_RELEASE mechanics — guide §3.11 intact);
   `triage.coverage_check` max **L3** (Mode A is human by construction);
   `triage.ex_gratia` max L1 **permanent**; launch levels as registration
   `initial_level` (`intake.claim_creation` L2; every other §5.6 row L1) —
   registration, not promotion; register #78's L0→L1 gate untouched.
4. **Template registry corrections (§5.5 — proposed #124):** add `T-06a`
   (email, `pending_capture`, blocked on item 6); `T-06a`/`T-06`
   `min_verification: extracted` per §5.5 (no money figures present).
5. **`packs/motor/cop_steps.yaml`** — `intake.claim_creation` step ids
   verbatim (`create_claim, ingest, populate, dupe_check, late_check,
   acknowledge, checklist, triage`), data only in this slice; PACKET-14
   executes them; feeds G-PROC (#67, which otherwise stays pending).

**Out / visibly blocked:** intake flow S1–S8 execution, dupe/late checks,
triage §5.4 (PACKET-14); Graph poller/webhook, real sends, archive-label
writes, self-address capture (item 1); T-06a/T-06/T-07 bodies (item 6);
R-10 threshold (#50), R-11 waiver conditions (item 4); Mode B (PRD-09
`icon.read`); live classifier prompt tuning (needs corpus, item 7); G-PROC
implementation (needs agent runs at volume — stays pending); scenarios
1/3/4/6 (PACKET-14); durable send-window queue (item-1 transport packet).

## 2. Binding spec quotes (implement verbatim)

Section 0.5 AR-2:

> "Every side-effectful action goes through one function:
> `execute_or_stage(capability_id, action, claim_id) →` at L0 log only; L1
> create `DRAFT_RELEASE` review item; L2 create typed confirm item; L3
> execute + maybe-sample into review; L4 execute. Critical grader failure on
> the action's payload forces the L1 path regardless of level (PRD-03 §3.3
> gating rule). Agents contain **zero** direct sends/writes outside this
> gate."

Section 0.5 AR-1:

> "A run that emits a review item moves to `awaiting_review` and **ends its
> turn**; resolution events resume it. No agent ever blocks a worker waiting
> on a human."

Section 0.5 AR-3a:

> "**08:00–18:00 EAT, Mon–Sat, Kenya public holidays excluded** ...
> Acknowledgements (`intake.acknowledge`) are exempt — they send 24/7."

PRD-05 §5.2:

> "**Final classifier contract (v1.1 ... the enum is exactly these five
> classes)**" [table with ≥ 0.85 / ≥ 0.95 boundaries]

> "**Mail is never silently dropped:** auto-archived items remain queryable
> and digest-listed."

> "Idempotency: `graph_message_id` unique constraint; redeliveries are
> no-ops. Loop guard: never process mail sent by ourselves (from-address
> check) — hard-coded."

> "a document/reply arriving for a claim in a terminal state is **always
> attach + timeline note + owner notification, never a state change**."

PRD-05 §5.7:

> "(2) reply on an existing thread attaches to the right claim with zero new
> claims created across 50-email replay; ... (5) self-sent mail ignored
> (loop test)"

PRD-05 §5.8:

> "**Weighted round-robin auto-assignment at `claim.created`** (weight =
> open-claim count per officer), implemented as event consumer `assigner`,
> writes `claims.assigned_to`."

## 3. Deliverable

```text
platform/agent_runtime/
  __init__.py      # build_agent_runtime(app, *, grade=None) -> runtime; Action
  models.py        # agent_runs, AR-1 DDL verbatim, on claim_core Base
  runner.py        # step runner, resume, heartbeats, reaper Beat task
  gate.py          # execute_or_stage; forbidden funds-transfer set; sampling
  comms.py         # AR-3 send(); G-COMM deterministic checks; window decision
packs/motor/agent_runtime/gate.yaml   # confirm_types map; exempt_capabilities
packs/motor/cop_steps.yaml            # intake.claim_creation S1–S8 (data)
packs/motor/intake/intake.yaml        # self_addresses, archive sample_rate
agents/intake_agent/
  __init__.py      # build_intake_agent(app, *, classifier=None, officers=None,
                   #                    config=None)
  router.py        # §5.2 strict-order routing
  assigner.py      # §5.8 weighted round-robin
  classifier.py    # AR-4 LIGHT classifier; injectable seam; prompt = pack data
platform/claim_core/alembic/versions/0010_agent_runs.py
```

**Authorised existing-package changes, exactly these:**

1. `ledger.ACTION_MAP` gains `claim.assigned` (#128; precedent #70/#94/#109).
2. `eval_harness` grader registry: G-COMM `PendingGrader` replaced by the
   implemented deterministic grader (#129); registry surface otherwise
   unchanged; no `grader_map.yaml` OutputType change without a matching
   entry.
3. `claim_core` gains **one** curated write method for inbound
   `communications` rows (the router's attach path) if none exists; no other
   claim_core change.
4. Pack data edits pinned above (§1.3/§1.4/§1.5) and, in
   `packs/motor/notify/notify.yaml` + the notify digest summary, the
   terminal-inbound owner-notification rule and the archived-mail digest
   count (#122/#127) — config plus the minimal digest-summary read.

`.github/`, `tools/ci/`, protected acceptance files untouched by the builder.
No new CI legs (Python only).

### 3.1 Pinned public surface (acceptance relies on exactly this)

```python
from agent_runtime import Action, build_agent_runtime
from intake_agent import build_intake_agent

runtime = build_agent_runtime(app, grade=None)
#   after build_cop_runtime + build_eval_harness + build_review_queue
#   grade: callable(action, capability_id, claim_id) -> object with .blocked
#          (None wires the PACKET-08 eval_harness grade_output seam)

runtime.register_executor("test.echo", fn)          # funds-transfer ids raise
outcome = runtime.execute_or_stage(
    capability_id="intake.acknowledge",
    action=Action(type="test.echo", payload={...}),
    claim_id=claim_id,
    actor="agent:intake",
)
# -> {"status": "logged"|"staged"|"executed"|"refused",
#     "review_id": ... | None, "review_type": ... | None, "sampled": bool}

result = runtime.comms.send(
    template_id="T-06a", claim_id=..., to_party_ids=[party_id],
    attachments=(), capability_id="intake.acknowledge", actor="agent:intake",
)
# -> {"status": "staged"|"refused"|"queued_window",
#     "code": None|"G_COMM_FAILED"|"TEMPLATE_NOT_REGISTERED", "review_id": ...}

agent = build_intake_agent(
    app,
    classifier=fake,   # .classify(message: dict) -> {"class": str, "confidence": float}
    officers=None,     # None loads claims_officer actors from roles.yaml
    config=None,       # None loads packs/motor/intake/intake.yaml; a dict
                       # ({"self_addresses": [...], "archive_sample_rate": int})
                       # overrides for tests, notify-config pattern
)
```

`Action(type, payload, grader_id=None)`: the gate consults `grade` (PRD-03
§3.3) whenever `grader_id` is set; a blocked decision forces the L1 path.

`email.received` payload (pinned): `{"graph_message_id", "conversation_id",
"from_addr", "to_addrs": [...], "subject", "body_text",
"attachments": [{"filename", "mime", "content_b64"}]}`. Router and assigner
are dispatcher consumers, synchronously drivable with `dispatch_once()`.

## 4. CTO decisions (D-x) and proposed register entries

Builder appends with the implementation PR:

- **#119 — package naming** (mirror #18/#33/#46/#66): shared runtime =
  `platform/agent_runtime/`; the agent = `agents/intake_agent/` (import
  `intake_agent`; `agents/` is already on pythonpath).
- **#120 — `email.received` has no producer** (item 1). The router consumes
  durable events; the Graph poller/webhook (AR-3b) and `self_addresses`
  capture land with the item-1 transport packet. Until then `self_addresses`
  may be empty — safe because no outbound transport exists to create a loop;
  the transport packet must refuse to enable sending while the list is
  uncaptured.
- **#121 — §5.2 routes confident `new_intimation` into the S1–S8 flow, which
  is PACKET-14.** The router emits a durable `intake.requested` event and
  creates nothing else; PACKET-14's runner consumes it. No claim is created
  by classification in this slice.
- **#122 — terminal-inbound narrowing.** "Substantive new evidence" is
  narrowed to attachment-bearing inbound (REOPEN_PROMPT on
  DECLINED/WITHDRAWN); the SETTLED/CLOSED money-relevant `EXCEPTION` requires
  a doc-type signal and lands with PACKET-14's classification consumer.
  Capture confirmation wanted on the narrowing.
- **#123 — §5.6 supersedes the provisional #71 seed** for its seven rows:
  `intake.doc_request` added; `triage.decline_draft` max L2;
  `triage.coverage_check` max L3; `triage.ex_gratia` max L1 permanent; §5.6
  launch levels are registration `initial_level`s, not promotions — #78's
  L0→L1 fail-closed gate is untouched.
- **#124 — template registry corrections per §5.5**: `T-06a` was never
  registered; `T-06a`/`T-06` `min_verification` is `extracted` (the
  provisional `human_verified` value predates PRD-05's explicit statement).
  Bodies remain `pending_capture` (item 6).
- **#125 — AR-1a "ops alert" has no transport** (item 1): a thrice-failed
  step yields `status='failed'` + `EXCEPTION{agent_run_failed}` review item —
  visible in the queue; a paging/email alert channel arrives with item 1.
- **#126 — AR-2's L2 "typed confirm item" mapping is unstated.**
  `gate.yaml confirm_types` pack data, closed-enum members only; an unmapped
  capability at L2 fails closed to the L1 draft path — never an invented
  type.
- **#127 — archive semantics before Graph exists.** `mail.archived` event +
  queryable row + deterministic hash sampling (rate pack config, 10% launch)
  + archived-mail count in the notify digest; the Graph label write is
  item-1 blocked.
- **#128 — assignment ledger coverage.** `claim.assigned` joins ACTION_MAP;
  weight ties break lexicographically (deterministic, no RNG in agents).
- **#129 — G-COMM becomes implemented** as AR-3's deterministic checks
  (recipient ⊆ claim parties; verification floors enforced by the
  StrictUndefined render; template registered), severity `critical`
  (PACKET-08 registration), closing the G-COMM arm of #67. G-PROC stays
  pending.
- **#130 — AR-3a durable window queue deferred.** This slice computes the
  window decision and returns a visible `queued_window` outcome; the durable
  queue + release job ship with the item-1 transport packet.
  `intake.acknowledge` is exempt via pack config.

## 5. Builder guardrails

- **AR-2 is the only door** — zero direct sends/writes in `agents/` or
  `agent_runtime` outside `execute_or_stage`; `banned_calls.py` stays green;
  `notify/` remains the only whitelisted bypass (staff only).
- **No payment execution** — the forbidden action-type set is hard-coded;
  no funds-transfer executor, ever; GP-1 posture unchanged.
- **Never guess** — ambiguous match ⇒ `EXCEPTION{ambiguous_inbound}`;
  unmapped L2 confirm type ⇒ L1; pending template ⇒ visible pending draft;
  missing transport ⇒ visible blocked state; classifier below boundary ⇒
  `DOC_CLASSIFY{mailbox_triage}`. No silent default anywhere.
- **Mail never silently dropped** — every non-self inbound leaves a
  queryable row/event; self-sent mail leaves the redelivery guard row only
  if already recorded, otherwise nothing (loop guard is a skip, not a drop
  of third-party mail).
- **Closed enums** — 17 review types, five classifier classes, AR-1 status
  set: exact; new cases are `EXCEPTION` subtypes.
- **Append-only writes** — attachment/document/field writes via existing
  claim service methods; no in-place updates; `human_verified` supersession
  409 untouched.
- **Autonomy ceilings** — §5.6 maxes enforced by registration; promotion
  beyond max denied by the untouched PACKET-08 validator; `triage.ex_gratia`
  L1 permanent; approval authority is not a capability.
- **Config over code** — classifier prompt, boundaries live in the §5.2
  table (spec constants, cite them), sample rate, self addresses, confirm
  types, exempt capabilities, step definitions: pack/org data. The loop
  guard's *mechanism* is hard-coded per §5.2; its *addresses* are config.
- **Determinism** — no RNG in agents; sampling and tie-breaks hash/order
  based.
- All PACKET-01–12 suites keep passing unmodified.

## 6. Definition of done (ED-7/ED-7a)

- `tests/acceptance/test_packet_13_agent_runtime.py` passes unmodified on
  SQLite and PostgreSQL legs; full suite green.
- Scenarios 2 and 5 implemented verbatim (50-email replay, zero new claims;
  self-sent ignored), including the 0.85/0.95 classifier boundaries
  inclusive.
- ≥80% coverage on `platform/agent_runtime/` and `agents/intake_agent/`;
  migration reviewed (single `agent_runs` table); OpenAPI unchanged or
  regenerated if routes were added (none are pinned); runbook content flagged
  in the PR (run recovery/reaper triage, router misroute triage, gate
  outcomes, window decisions).
- Grader coverage: G-COMM flips pending → implemented; `grader_map.yaml`
  consistent — confirm explicitly in the PR description.
- ED-11: further ambiguity ⇒ narrowest safe behaviour + proposed register
  entry; stop and flag before expanding this packet.
