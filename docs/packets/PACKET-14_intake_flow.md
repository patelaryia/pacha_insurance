# PACKET-14 — Intake flow S1–S8 as a durable COP run, Mode A triage, decline path, KPI wire (PRD-05 slice 2 of 2)

> **Status:** issued · **Builder:** Codex per `AGENTS.md` · **Reviewer:** CTO per `CLAUDE.md`
> **Source spec:** `docs/PRD-05_Intake_and_Triage_Agent_v1.1.md` §5.3 (S1–S8),
> §5.4 (Mode A coverage/premium/excess triage, decline path), §5.5 (T-06/T-06a/
> T-07 ownership), §5.7 scenarios 1/3/4/6 + KPI, §5.2 terminal-inbound
> money-relevant arm; Section 0.5 AR-1/AR-1a/AR-2/AR-3; PRD-00 §0.4 (FSM,
> decline action), §0.5 (acknowledge clock); PRD-02 C-01/R-02/R-03/R-10;
> PRD-03 §3.3; PRD-04 §4.3 (closed enum, resolution schemas); Section 0
> ED-8/ED-11; guide §3.5/§3.11/§6; registers #29/#43/#50/#56/#63/#97/#103/
> #107/#121/#122/#126/#130/#132.
> Precedence: Section 0 → Section 0.5 → PRD-00/PRD-02/PRD-03/PRD-04/PRD-05 →
> PACKET-06..13 contracts → this packet.
> **Depends on:** PACKET-13 merged on main (including registers #119–#133).
> **Acceptance:** `tests/acceptance/test_packet_14_intake_flow.py` —
> protected, failing by design until this packet is built.
> **Packet 15 (next):** PRD-06 document-chase agent (consumes the `chase.init`
> hand-off this packet emits).

## 0. Slice boundary

PACKET-13 shipped the shared runtime (AR-1/AR-2/AR-3) and the deterministic
half of PRD-05: the §5.2 router now emits a durable `intake.requested` event
for confident `new_intimation` mail and creates nothing else (#121). This
packet consumes that event and executes §5.3 verbatim as a durable
`agent_runs` COP run — S1 create_claim through S8 triage — plus the §5.4
Mode A triage card, the R-02 decline path with T-07, the S4 dupe/S5 late
checks, the §5.2 SETTLED/CLOSED money-relevant arm deferred by #122, and the
§5.7 intimation→acknowledgement KPI wire. Scenarios 1, 3, 4 and 6 (scenarios
2 and 5 shipped in PACKET-13).

**Still no live Graph** (open item 1): T-06a/T-06/T-07 bodies are
`pending_capture` (item 6) and the AR-3 transport slot is `pending_capture`.
Every outbound in this packet terminates in a **visible** staged draft,
pending-template draft, or refused-transport state — never a fake
`email.sent`. The KPI series is live and shows zeros until a real send
exists; it never fabricates a duration.

## 1. Scope

**In:**

1. **`agents/intake_agent/flow.py` — the S1–S8 run.** A dispatcher consumer
   **`intake_flow`** on `intake.requested` starts an `agent_runs` run
   (`agent='intake'`, `capability_id='intake.claim_creation'`,
   `trigger_event` = the `intake.requested` event id, `autonomy_level`
   snapshot per AR-1) and registers all eight step callables on the shared
   runner, ids and order verbatim per `packs/motor/cop_steps.yaml`
   (`create_claim, ingest, populate, dupe_check, late_check, acknowledge,
   checklist, triage`). Steps are idempotent; re-invocation never duplicates
   a claim, document, party, review item, or event. One `intake.requested`
   event → at most one run → at most one claim (idempotency key =
   `graph_message_id`).
   - **S1 `create_claim` — governed.** Claim creation is the capability's
     side effect and goes through `execute_or_stage` (registered internal
     executor `intake.create_claim`). `gate.yaml confirm_types` has no
     mapping for `intake.claim_creation`, so at the §5.6 launch level L2 the
     gate **fails closed to the L1 `DRAFT_RELEASE` path** (#126): the run
     pauses `awaiting_review`; officer approval executes the creation and
     resumes (proposed #135). At L3+ it executes directly. The claims row:
     `lob='motor'`, pack pinned, FSM `INTIMATED`; the acknowledge SLA clock
     starts on `claim.created` (§1.6 below).
   - **S2 `ingest`:** the email body becomes a synthetic `intimation_email`
     document (created **first**, then attachments in payload order — the
     acceptance suite scripts model calls in that order); each attachment
     becomes a documents row; every row emits `document.received` and enters
     the PRD-01 pipeline. Same-bytes re-sends follow #132
     (`INBOUND_DUPLICATE_ATTACHMENT` timeline entry, no exception).
   - **S3 `populate`:** returns the new **waiting** outcome
     (`{"status": "waiting", "expects_event": "document.extracted"}`,
     proposed #134) until the synthetic `intimation_email` document has
     extracted; attachments commit whenever their own extraction lands.
     Then: parties created from the sender + extracted broker/insured via
     the new curated `claim_core` party method; §5.3's "intimation.date" is
     written to the registered **`intimation.received_at`** path (proposed
     #144) as `source_type='system'`, confidence null, value = the
     `email.received` timestamp. Photos-only intimation (no narrative):
     claim still created; `loss.narrative` absence is recorded for S7.
   - **S4 `dupe_check`:** (a) non-terminal claims, same current
     `vehicle.reg` AND `loss.date` within ±3 days **inclusive** →
     `EXCEPTION{subtype: possible_duplicate}` with the candidate claim-id
     list; run pauses (`awaiting_review`; resolution resumes). (b) terminal
     claims (`SETTLED, CLOSED, DECLINED, WITHDRAWN, VOID`), same reg +
     loss.date ±3d, terminal transition within 365 days **inclusive** →
     **never a pause**: durable `fraud.signal` event (proposed #136),
     surfaced on the S8 coverage card payload; the approval-note
     verification slot is PRD-08 scope. Missing committed `vehicle.reg` or
     `loss.date` → the step records a visible `insufficient` outcome and
     continues — absence of evidence is not a duplicate and not a pause
     (mirror #43; proposed #137).
   - **S5 `late_check`:** evaluate R-10. R-10 remains `blocked_on_inputs`
     on `pack.late_days` (#50): the evaluation is recorded as a visible
     blocked `rule_runs` row and the run **continues** — a blocked rule is
     not a run pause (proposed #138). The §5.3 unverified-`loss.date`
     condition resolves to the same visible blocked semantics.
   - **S6 `acknowledge`:** AR-3 `send(T-06a, …)` at capability
     `intake.acknowledge` (window-exempt 24/7 per AR-3a), recipients = the
     sender party (the reply lands on the intimation thread; Reply-To is the
     shared mailbox per AR-3). At launch L1 the send stages a
     `DRAFT_RELEASE` with the visible pending-template body (item 6); at L3
     the execute path terminates at the visible refused-transport /
     pending-template state (#120/#130). No path invents prose or a fake
     send.
   - **S7 `checklist`:** emit one durable **`chase.init`** event (event-
     catalog addition mirroring #121; proposed #147). Payload: the
     instantiated checklist — base 7 items verbatim §5.5
     (`claim_form, logbook_copy, dl_copy, kra_pin, police_abstract,
     repair_estimate, photos`, ids from pack
     `packs/motor/intake/intake.yaml checklist_base_items`, with the
     item→accepted-document mapping in `checklist_doc_types`), each with
     `already_received` computed from documents held on the claim; the
     police abstract is **included** while R-11 is `blocked_on_inputs`
     (item 4 — an unevaluable waiver fails closed to include); missing
     `loss.narrative` appends the conditional `incident_description` item
     (§5.3). No checklist table — persistence and sends are the PRD-06
     packet.
   - **S8 `triage` — Mode A only** (PRD-09 `icon.read` does not exist; Mode
     A is human by construction, `triage.coverage_check` max L3): create
     `FIELD_VERIFY{subtype: coverage_manual}` — the coverage checklist card
     (proposed #142): extracted policy number if held, the six §5.4 keyed
     paths (`policy.sum_insured, policy.period_start, policy.period_end,
     policy.endorsement_ref, policy.premium_paid, policy.excess_protector`),
     any `fraud.signal` payloads from S4b. Run pauses. Resolution (schema
     `FIELD_VERIFY_COVERAGE@1`) writes every supplied path as one
     transaction of append-only `human` source versions carrying the review
     citation (#107 pattern); blank optional fields are **not written**,
     never defaulted. Resume → deterministic sequence verbatim §5.4:
     - **C-01** → `policy.excess_amount` (existing calc, `source_type='calc'`);
     - `loss.date` outside `[policy.period_start, policy.period_end]` →
       `EXCEPTION{subtype: out_of_cover}`;
     - `policy.premium_paid == false` → `EXCEPTION{subtype: premium_unpaid}`
       (officer decision);
     - **R-02** (`estimate <= excess`, boundary **equal fires**) → decline
       path: existing `propose_decline` outcome →
       `DRAFT_RELEASE{subtype: decline_draft, draft_template: T-07}`
       (subtype label per #63 and the protected PACKET-07 fixture; proposed
       #140) **plus** R-03 → `EX_GRATIA` routed `claims_manager`
       (capability `triage.ex_gratia`, L1 permanent). Decline context
       fields (`client.loss_ratio`, `client.premium_history`) remain
       unregistered per #56: the card shows visible
       `pending_field_registration` markers (proposed #143).
     - If the estimate is not yet held, R-02 persists
       `blocked_on_inputs` and S8 still completes `INTIMATED → TRIAGED`;
       PRD-06 obtains the estimate later. The missing estimate is not an
       exception or a coverage-triage precondition (#149).
     - The clean path and the below-excess path end with
       `INTIMATED → TRIAGED` (coverage + excess evaluated — PRD-00 §0.4);
       `out_of_cover` / `premium_unpaid` pause **before** the transition —
       the officer decides.
     - **Scenario-3 posture (proposed #139):** nothing is auto-released or
       auto-withdrawn on the decline path. The T-06a receipt-ack precedes
       triage by §5.3 order and stays a human decision; "no
       acknowledgement-of-processing" = the platform releases no outbound on
       a below-excess claim; DECLINED chase suppression is PRD-06 §6.4.
2. **Decline release wiring (proposed #141):** `review_queue` resolution of
   `DRAFT_RELEASE{decline_draft}`: **approve** pre-validates the claim is
   `TRIAGED`, renders T-07 — `pending_capture` (item 6) ⇒
   `409 RESOLUTION_BLOCKED_ON_INPUTS` naming T-07, item stays open (mirror
   #97) — on success (post-capture) commits `decline(below_excess)` →
   `DECLINED` (red-banner, PRD-04) and stages the letter via AR-3 under
   `triage.decline_draft` (max L2; release always human, guide §3.11).
   **Reject** leaves `TRIAGED` unchanged with the structured reason.
3. **Terminal money-relevant arm (#122 close-out; proposed #148):** consumer
   on `document.classified` for claims in `SETTLED`/`CLOSED`: doc type ∈
   pack `money_relevant_doc_types` → `EXCEPTION` routed `claims_manager`.
   The PRD-01 motor registry contains no invoice/demand doc type, so the
   list ships **`pending_capture`** — the slot and consumer exist, terminal
   inbound stays attach + notify (PACKET-13 behaviour) visibly until the
   types are captured.
4. **KPI wire (§5.7; proposed #145):** acknowledge SLA clock goes **live**
   in `platform/claim_core/sla/definitions.yaml` (start `claim.created`,
   warn 30m, breach 2h, 24x7 — PRD-00 §0.5 verbatim), `stop_event` pinned =
   `email.sent` with `template_id: T-06a` for the claim (closes the #29 gap
   for this clock only); `escalate_to_role` stays `pending_capture`.
   YAML representation: `stop_event: email.sent` +
   `stop_filter: {template_id: T-06a}` on the acknowledge row.
   Dashboard series **`intimation_to_acknowledgement`** (live,
   `eat_calendar`) in `packs/motor/dashboard.yaml` + `ops_reads` reader +
   CSV export: count and median minutes over **stopped** acknowledge
   clocks; zeros when empty (#79 precedent). No real send can exist before
   item 1 — the series is visibly zero, never fabricated.
5. **Pack data edits, exactly these:** `packs/motor/fields.yaml` gains
   `policy.period_start` (date), `policy.period_end` (date),
   `policy.endorsement_ref` (string), `policy.premium_paid` (bool),
   `policy.excess_protector` (bool) — names verbatim §5.4, no rule consumes
   `excess_protector` yet (semantics uncaptured, proposed #142);
   `packs/motor/review/contracts.yaml` gains the `FIELD_VERIFY` subtype
   slot `coverage_manual` (`workspace_layout: coverage_checklist_card`,
   `resolution_schema: FIELD_VERIFY_COVERAGE@1`) +
   `packs/motor/review/schemas/FIELD_VERIFY_COVERAGE@1.json` (keeps the
   #93 capability/diff correction core, adds the required `fields` object);
   `packs/motor/intake/intake.yaml` gains `checklist_base_items` (the §5.5
   seven, data) and `money_relevant_doc_types: pending_capture`;
   `packs/motor/dashboard.yaml` gains the series row above.

**Out / visibly blocked:** Graph transport, real sends, archive labels,
self-address capture (item 1); T-06a/T-06/T-07 bodies (item 6) — ack drafts
render pending-template, decline release 409s; R-10 threshold (#50); R-11
waiver conditions (item 4); Mode B (PRD-09 `icon.read`); `client.*` decline
context fields (#56); money-relevant doc types (proposed #148); checklist
persistence, doc-request sends, chase reminders (PRD-06 packet); G-PROC
(needs run volume — stays pending); wall-clock <5 min / <15 min gates
(live trial, proposed #146); `salvage`/settlement anything.

## 2. Binding spec quotes (implement verbatim)

PRD-05 §5.3:

> "S4 dupe_check (a) OPEN claims, same vehicle.reg AND loss.date ±3d →
> EXCEPTION{type: possible_duplicate}, run pauses. (b) v1.1: CLOSED claims,
> same reg + loss.date ±3d, within 365 days → NOT a pause: FRAUD_SIGNAL info
> flag on the claim, surfaced in triage and in the approval note's
> verification section."

> "`new_intimation` with **photos only, no narrative** (observed pattern):
> claim still created; S3 marks `loss.narrative` missing → the doc request
> (S7) includes 'description of the incident' as a chase item."

PRD-05 §5.4:

> "**Mode A — no ICON read (launch):** agent creates review item
> `FIELD_VERIFY{subtype: coverage_manual}` presenting a **coverage checklist
> card**: policy number (extracted), fields for the officer to key from ICON
> — `policy.sum_insured`, `policy.period_start/end`,
> `policy.endorsement_ref`, `policy.premium_paid (bool)`,
> `policy.excess_protector (bool)`. These land as `human` source fields."

> "Then, deterministically: `C-01` excess → `policy.excess_amount`; loss
> date within period else `EXCEPTION{type: out_of_cover}`; premium unpaid →
> `EXCEPTION{type: premium_unpaid}` (officer decision, pack note records
> outcome); `R-02` below-excess → **decline path** ... Release of a decline
> transitions FSM → DECLINED."

PRD-05 §5.7:

> "(1) Synthetic intimation email → claim created, fields populated w/
> citations, acknowledgement drafted, checklist live, all < 5 min
> wall-clock; ... (3) below-excess corpus case → decline draft + ex-gratia
> item, no acknowledgement-of-processing sent; (4) duplicate reg+date →
> pauses with exception; ... (6) at `intake.acknowledge`=L3, ack lands in
> test inbox < 15 min from intimation with zero human touches. KPI wired to
> dashboard: intimation→acknowledgement (baseline 8 days; target < 15 min)."

PRD-00 §0.5:

> "| acknowledge | **24x7** (confirmed intended; L3 ack runs round the clock
> and the trial metric counts it) | start `claim.created`, warn 30m,
> breach 2h |"

Section 0.5 AR-1:

> "A run that emits a review item moves to `awaiting_review` and **ends its
> turn**; resolution events resume it. No agent ever blocks a worker waiting
> on a human."

## 3. Deliverable

```text
agents/intake_agent/
  flow.py         # intake_flow consumer; S1–S7 step callables; run start
  triage.py       # S8 Mode A card + deterministic §5.4 sequence + decline path
packs/motor/review/schemas/FIELD_VERIFY_COVERAGE@1.json
docs/runbooks/intake_flow.md
```

plus the pinned data edits (§1.5) and the authorized package changes below.
**No new table and no Alembic migration.** The resolve transport is the existing
`POST /reviews/{id}/resolve`; the §3.1 bare `/portfolio` compatibility reads are
the only added routes and are registered for consolidation at #153.

**Authorised existing-package changes, exactly these:**

1. `claim_core`: (a) **one** curated party-create method (S3); (b) **one**
   curated dupe-lookup read — claims by current `vehicle.reg` value +
   `loss.date` window + terminal/non-terminal class (S4); (c) SLA engine
   stop-on-event support + the acknowledge-clock activation in
   `sla/definitions.yaml` (§1.4); (d) `ledger.ACTION_MAP` gains
   `chase.init` and `fraud.signal` (precedent #70/#94/#109/#128). Nothing
   else.
2. `agent_runtime`: the runner **waiting-step** semantics only (#134) — a
   step may return `{"status": "waiting", "expects_event": <type>}`; the
   run stays `running`, the step is incomplete, the return is **not** a
   failed attempt; the existing `agent_runtime` dispatcher consumer
   re-invokes the run when that event type arrives; the reaper remains the
   stall backstop.
3. `review_queue`: the `coverage_manual` resolution handler (one-transaction
   multi-path human write, #107 citation pattern); the
   `DRAFT_RELEASE{decline_draft}` release handler (#141); the
   `intimation_to_acknowledgement` series reader in `ops_reads`.
4. No `eval_harness` change; no `grader_map.yaml` change (no new
   OutputType); G-PROC stays pending.

**Implementation inventory beyond the initial authorised-change list:**
`agent_runtime` passes the owning workflow `run_id` through the gate and exposes
`execute_staged` so an approved S1 action can commit without a second autonomy
decision; `cop_runtime/outcomes.py` adds the decline capability attribution and
#143 pending-field markers; `doc_intel/commit.py` carries the already-validated
`citation_mode` into source provenance. These are enumerated for review rather
than treated as implicit scope; the remaining per-action autonomy snapshot gap is
registered at #154.

`.github/`, `tools/ci/`, protected acceptance files untouched by the
builder. No new CI legs.

### 3.1 Pinned public surface (acceptance relies on exactly this)

- `build_intake_agent(app, classifier=…, officers=…, config=…)` — signature
  unchanged from PACKET-13; the call now **also** registers the eight
  `intake.claim_creation` steps on `app.state.agent_runtime` and the
  `intake_flow` dispatcher consumer on `intake.requested`.
- Run rows: `agent_runs.agent='intake'`,
  `capability_id='intake.claim_creation'`, `trigger_event` = the
  `intake.requested` event id; `steps` ids/order verbatim
  `cop_steps.yaml`.
- Waiting outcome (persisted in `steps[].outcome`):
  `{"status": "waiting", "expects_event": "document.extracted"}`.
- S2 document order: synthetic `intimation_email` body document first, then
  attachments in `email.received.attachments` order (model-call scripting
  depends on it).
- `chase.init` payload:
  `{"claim_id", "items": [{"id", "already_received": bool}, …]}` — ids
  `claim_form, logbook_copy, dl_copy, kra_pin, police_abstract,
  repair_estimate, photos` (+ `incident_description` appended when
  `loss.narrative` is uncommitted).
- `fraud.signal` payload:
  `{"claim_id", "matched_claim_id", "vehicle_reg", "loss_date",
  "matched_terminal_state"}`.
- `EXCEPTION{possible_duplicate}` review payload carries
  `"candidates": [claim_id, …]`.
- Coverage card review payload: `"keyed_paths"` = the six §5.4 paths;
  `"fraud_signals"` = the S4b `fraud.signal` payloads for the claim (empty
  list when none).
- Coverage card resolve:
  `POST /reviews/{id}/resolve` `{"action": "approve", "schema_version":
  "FIELD_VERIFY_COVERAGE@1", "payload": {"capability_id":
  "triage.coverage_check", "diff": {…}, "fields": {<path>: <value>, …}}}` —
  money in integer cents (ED-8), dates ISO `YYYY-MM-DD`, bools JSON.
- Coverage-card reject issues one fresh open `coverage_manual` item carrying
  `retry_of: <rejected item id>`; the run remains `awaiting_review` and no
  exception subtype is created.
- Decline release while T-07 is pending: resolve approve returns **409**
  with `code == "RESOLUTION_BLOCKED_ON_INPUTS"` and the item stays open.
- `GET /portfolio` includes `intimation_to_acknowledgement` with
  `status: "live"`; `GET /portfolio/intimation_to_acknowledgement.csv`
  returns 200; data = `{"count": int, "median_minutes": number|null}` over
  stopped acknowledge clocks.

## 4. CTO decisions (D-x) and proposed register entries

Builder appends with the implementation PR. Numbering note: **#131 is an
unassigned gap** from the PACKET-13 round — do not reuse it; this packet's
entries are **#134–#148**.

- **#134 — AR-1 `expects_events` needs runnable wait semantics.** A step may
  return `waiting{expects_event}`; run stays `running`, not an attempt, the
  dispatcher consumer re-invokes on that event; reaper unchanged as
  backstop.
- **#135 — S1 claim creation is the governed side effect.** Routed through
  `execute_or_stage` (executor `intake.create_claim`). `gate.yaml` maps no
  L2 confirm type for `intake.claim_creation` → fails closed to the
  `DRAFT_RELEASE` path (#126) at the §5.6 launch level; §5.6's "creation is
  reversible" note hints at execute-then-confirm semantics but AR-2
  verbatim wins. Capture confirmation wanted on a proper confirm mapping.
- **#136 — FRAUD_SIGNAL is not one of the 17 review types.** Implemented as
  the durable `fraud.signal` claim event + ACTION_MAP entry, surfaced on
  the S8 coverage card; the approval-note verification slot lands with
  PRD-08.
- **#137 — dupe-check pins.** ±3 days inclusive; "OPEN" = non-terminal
  states; "CLOSED" = the five terminal states with terminal transition
  within 365 days inclusive; missing reg/loss.date → visible
  `insufficient`, never a pause (mirror #43). Capture confirmation wanted
  on the terminal-state set.
- **#138 — a blocked rule is not a run pause.** S5 records R-10's
  `blocked_on_inputs` `rule_runs` row (#50) and continues; same for the
  §5.3 unverified-loss.date condition.
- **#139 — scenario-3 "no acknowledgement-of-processing".** Read as: the
  platform auto-releases no outbound on the decline path. The T-06a
  receipt-ack precedes triage by §5.3 step order and remains a human
  decision (never auto-released, never auto-withdrawn); DECLINED chase
  suppression is PRD-06 §6.4 scope. Capture confirmation wanted.
- **#140 — decline subtype label.** PRD-05 §5.4 says
  `DRAFT_RELEASE{subtype: decline}`; #63 shipped `decline_draft` and the
  protected PACKET-07 fixture asserts it. The label stays `decline_draft`;
  reconciliation goes to the next spec round.
- **#141 — decline release wiring.** Approve pre-validates `TRIAGED`,
  renders T-07; `pending_capture` ⇒ `409 RESOLUTION_BLOCKED_ON_INPUTS`,
  item stays open (mirror #97); post-capture success commits
  `decline(below_excess)` → `DECLINED` + letter staged via AR-3 under
  `triage.decline_draft`; reject leaves `TRIAGED`.
- **#142 — coverage card contract.** `FIELD_VERIFY{coverage_manual}`,
  layout `coverage_checklist_card`, schema `FIELD_VERIFY_COVERAGE@1` via a
  contracts.yaml subtype slot; six keyed paths registered as pack fields
  (§5.4's "five keys" prose counts the period pair as one); one-transaction
  `human` writes with the review citation (#107); blank optionals never
  written; `policy.excess_protector` stored with **no** consuming rule —
  semantics uncaptured, capture confirmation wanted.
- **#143 — decline context fields stay unregistered** (#56):
  `client.loss_ratio`/`client.premium_history` render visible
  `pending_field_registration` markers on the decline card.
- **#144 — §5.3 "intimation.date"** is written to the registered
  `intimation.received_at` path (`source_type='system'`, confidence null);
  no new dictionary path is invented.
- **#145 — acknowledge clock + KPI.** Clock live with `stop_event` =
  `email.sent{template_id: T-06a}` (closes the #29 stop-event gap for this
  clock only); `escalate_to_role` stays `pending_capture`; series
  `intimation_to_acknowledgement` live with zeros-when-empty (#79); no
  fabricated durations before item-1 transport.
- **#146 — wall-clock gates are live-trial gates** (mirror #83/#104): CI
  proves scenario-1 mechanics (pipeline to ack draft + checklist +
  citations) and scenario-6 mechanics (L3 zero-human path terminating at
  the visible refused-transport/pending-template state); the <5 min and
  <15 min inbox numbers are measured in the trial.
- **#147 — `chase.init` joins the event catalog** (mirror #121) carrying
  the instantiated checklist payload; base-7 ids are pack data; police
  abstract included while R-11 is blocked (fail-closed include);
  persistence is the PRD-06 packet.
- **#148 — money-relevant doc types are uncaptured.** The §5.2
  SETTLED/CLOSED arm ships as a `document.classified` consumer + pack
  `money_relevant_doc_types: pending_capture` (no invoice/demand type
  exists in the PRD-01 motor registry); terminal inbound stays
  attach + notify visibly until capture.

Review-round additions #149–#157 pin the no-estimate R-02 continuation,
creation-reject no-op, missing-sender insufficient outcome, coverage-card re-key,
portfolio-route consolidation, per-action attribution gap, checklist taxonomy
mapping, indexed duplicate-query follow-up, and staged-communication release
boundary. The master register is authoritative for their complete wording.

## 5. Builder guardrails

- **AR-2 is the only door** — S1 creation and every send go through
  `execute_or_stage`; zero direct sends/writes outside it;
  `banned_calls.py` stays green; no funds-transfer action type, ever.
- **Never guess** — dupe ambiguity ⇒ `EXCEPTION{possible_duplicate}`;
  missing dupe inputs ⇒ visible `insufficient`; R-10 ⇒ visible blocked;
  out-of-period ⇒ `EXCEPTION{out_of_cover}`; unpaid ⇒
  `EXCEPTION{premium_unpaid}`; pending template ⇒ visible pending draft;
  missing transport ⇒ visible refusal; uncaptured money-relevant types ⇒
  `pending_capture` slot. No silent default anywhere.
- **Closed enums** — 17 review types (new cases are subtypes above), five
  classifier classes, AR-1 status set, FSM state set: exact.
- **Append-only writes** — coverage-card resolution writes new `human`
  versions with review citations (#107); no in-place updates;
  `human_verified` supersession 409 untouched; no provenance, no
  `extracted` commit (PRD-01 §1.4).
- **Autonomy ceilings** — `triage.coverage_check` max L3 (Mode A human by
  construction); `triage.decline_draft` max L2, release human;
  `triage.ex_gratia` L1 permanent; no ceiling widens.
- **Money** — ED-8 integer cents end-to-end; C-01 stays Decimal
  `ROUND_HALF_EVEN` (#53); R-02 boundary `estimate == excess` **fires**.
- **Config over code** — checklist item ids, money-relevant types, dashboard
  series, confirm types, thresholds: pack data. The §5.4 deterministic
  sequence itself is spec logic and cites §5.4.
- **Determinism** — no RNG; idempotent steps; deterministic candidate
  ordering in `possible_duplicate` payloads (created-at, then id).
- All PACKET-01–13 suites keep passing unmodified.

## 6. Definition of done (ED-7/ED-7a)

- `tests/acceptance/test_packet_14_intake_flow.py` passes unmodified on
  SQLite and PostgreSQL legs; full suite green.
- Scenarios 1, 3, 4, 6 implemented verbatim, including the boundary cases
  (estimate = excess exactly; ±3-day inclusive dupe window; loss date on a
  period boundary is in cover).
- ≥80% coverage on `agents/intake_agent/`; no migration (assert none);
  OpenAPI unchanged; runbook content flagged in the PR (run recovery
  through S1–S8, dupe/fraud triage, coverage-card handling, decline
  release, KPI series).
- `grader_map.yaml` unchanged — confirm explicitly in the PR description;
  G-PROC remains pending with its `blocked_on` intact.
- ED-11: further ambiguity ⇒ narrowest safe behaviour + proposed register
  entry (next free number **#158**); stop and flag before expanding this
  packet.
