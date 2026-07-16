# PACKET-15 — Document-chase agent: checklists, matching, reminders, hard gates, analytics (PRD-06 complete)

> **Status:** issued · **Builder:** Codex per `AGENTS.md` · **Reviewer:** CTO per `CLAUDE.md`
> **Source spec:** `docs/PRD-06_Document_Chase_Agent_v1.1.md` §6.1–§6.7 (all);
> Section 0.5 AR-2/AR-3/AR-3a; PRD-00 §0.4 (suppression set), §0.5
> (`doc_item_age`); PRD-05 §5.5 (T-06 conditional blocks), §5.6
> (`intake.doc_request`); PRD-02 R-13/R-14; PRD-04 §4.3 (closed enum);
> Section 0 ED-8/ED-9 (chase rows never purged)/ED-11; guide §3.5/§3.11/§6;
> registers #29/#49/#61/#64/#75/#79/#111/#130/#145/#147/#155/#157.
> Precedence: Section 0 → Section 0.5 → PRD-00/PRD-02/PRD-04/PRD-05/PRD-06 →
> PACKET-06..14 contracts → this packet.
> **Depends on:** PACKET-14 merged on main (`64d4b00`, registers #134–#157).
> **Acceptance:** `tests/acceptance/test_packet_15_chase_agent.py` —
> protected, failing by design until this packet is built.
> **Packet 16 (next):** PRD-07 assessment orchestration (consumes
> `chase.complete` and the estimate documents this packet verifies).

## 0. Slice boundary

PACKET-14 ends intake at the `chase.init` hand-off. This packet builds the
whole of PRD-06 on the existing runtime: the two chase tables, the pack
checklist-item registry (`items.yaml`), inbound matching on
`document.classified`/`document.extracted`, the reminder engine (cadence,
recipient ladder, per-checklist deferral, cap-6 escalation, hard
suppression), the surrender hard-gate mode feeding the existing
R-13/R-14 `SURRENDER_CHECKLIST → SETTLEMENT` guard, and the chase analytics
series. Scenarios §6.7 (1)–(6) verbatim.

**Still no live Graph** (open item 1): T-06, T-06r-broker/T-06r-client
bodies are `pending_capture` (item 6 / #61) and the AR-3 transport is
`pending_capture`. Every request, reminder, and re-request terminates in a
visible staged draft / pending-template draft / refused-transport state
(#130/#157 posture unchanged). Reminder *cadence, selection, deferral,
suppression, and capping* are fully live and testable; only the transport
is blocked.

## 1. Scope

**In:**

1. **`agents/chase_agent/` (import `chase_agent`, proposed #158 — mirrors
   #119/#158-naming precedent).** Built by
   `build_chase_agent(app, *, config=None)` after `build_intake_agent`.
   Like the PACKET-13 router/assigner, checklist bookkeeping is an
   idempotent event consumer (no COP step sequence); every send is a
   gate-governed action whose durable record is the gate's own
   `agent_runs` row.
   - **Data model (§6.2 DDL verbatim):** `chase_checklists`
     (id, claim_id, purpose `'claim_docs'|'surrender'`, status
     `'open'|'complete'|'cancelled'`, blocking, created_at) and
     `chase_items` (id, checklist_id, item_id, state
     `'pending'|'requested'|'received'|'verified'|'rejected'|'waived'`,
     physical, requested_at, received_at, verified_at, waived_by,
     waiver_reason, reminder_count, next_reminder_at, document_id,
     reject_reason) **plus `snooze_until TIMESTAMPTZ`** — named by §6.4 but
     missing from the §6.2 DDL (proposed #161). On `claim_core.Base`;
     migration `0011_chase_checklists`; rows never deleted (ED-9).
   - **Checklist-item registry `packs/motor/checklists/items.yaml`
     (§6.2 comment, verbatim):** every item is
     `{id, kind: document|physical|field_request, doc_type?, target_path?,
     physical: bool}`. Registered ids: the seven base items
     (`claim_form, logbook_copy, dl_copy, kra_pin, police_abstract,
     repair_estimate, photos` with their PRD-01 doc types),
     `incident_description` (kind `field_request`, target
     `loss.narrative`), `logbook_original` + `keys_physical`
     (kind `physical`), `kra_pin_cert` (document), `bank_discharge_letter`
     (document), `cert_of_incorporation` (registered; **not**
     auto-instantiated — its auto-add condition is uncaptured, proposed
     #166). Instantiation **validates against this registry** — the 422
     path is closed by design. This registry **supersedes
     `intake.yaml checklist_doc_types`** (#155): the intake S7 checklist
     step reads `items.yaml`; the intake key is removed (proposed #165 —
     one doc-type map, not two).
   - **Instantiation consumer** on `chase.init` (PACKET-14 payload) →
     `purpose='claim_docs'` checklist, one row per payload item;
     `already_received: true` items enter `received` with `received_at` =
     instantiation time and the matching held document linked. Then the
     **initial T-06 document request** is staged via AR-3 at capability
     **`intake.doc_request`** (PRD-05 §5.6 "pairs with acknowledge" —
     PRD-06 names no sender for the first request, proposed #160),
     recipients = the intimation sender party; T-06 conditional blocks
     compiled from checklist state (§5.5): outstanding items listed,
     received items rendered "already received ✓", body `pending_capture`
     → visible pending-template draft. Outstanding items move to
     `requested`; `requested_at` = the gate outcome time (staged counts —
     launch levels stage everything, #160); `next_reminder_at` =
     `requested_at + first cadence step`.
   - **Surrender instantiation** on `claim.status_changed` →
     `SURRENDER_CHECKLIST` (proposed #173): `purpose='surrender',
     blocking=true`, items `{logbook_original, keys_physical,
     kra_pin_cert}` + `bank_discharge_letter` auto-added when committed
     `logbook.bank_interest.present` is true (§6.5, R-14). The field is
     registered pack data this packet; PRD-01's closed logbook extraction
     list has no `target_path` for it, so its launch source is
     human/officer-keyed (proposed #167). No outbound request is staged
     for surrender items — they are physical/officer-driven (§6.5).
2. **Inbound matching (§6.3):** consumer on `document.classified` for
   claims with an open checklist — exact `doc_type` match to an
   outstanding (`pending|requested|rejected`) item → `received` +
   `document_id` link; no-match documents stay attached with the existing
   timeline (never discarded — PACKET-13 already records every inbound).
   Verification (proposed #162, pack-mapped): doc types whose registered
   schema declares **no** `target_path` field (driving_licence,
   kra_pin_cert, photo_damage, police_abstract, discharge_voucher) have
   nothing to validate and verify on classification; target-bearing types
   verify on `document.extracted` with ≥1 committed field for that
   document, else **rejected** with the machine reason from the pack map
   (zero-commit extraction → `illegible`; a CC-1 **mismatch** result on
   that document → `wrong_vehicle` — an `insufficient` CC-1 never rejects;
   `expired`/`wrong_document` are reserved enum members with no producing
   signal yet — never guessed).
   Rejection stages a defect-specific **re-request** at capability
   `chase.rerequest` (launch L1) using the T-06r tone variant with a
   structured defect payload — §6.3 names no re-request template id
   (proposed #163); body `pending_capture` → visible pending draft, never
   invented prose. First state-advance wins; re-delivered events are
   no-ops. When every item of an open checklist is
   `verified|waived|received(physical-attested)` → checklist
   `complete` + `chase.complete` event.
3. **Reminder engine (§6.4):** public `app.state.chase_agent.tick(now=None)
   -> dict` (Beat every 15 min via the reaper's `configure_*` precedent;
   synchronous and clock-driven for tests). Selects open non-suppressed,
   non-snoozed checklists' outstanding items where
   `next_reminder_at <= now`:
   - **Cadence** (pack `chase/chase.yaml`, config): T+3d, T+7d, T+12d,
     then every 7d; **cap 6** per item → **one idempotent**
     `EXCEPTION{subtype: chase_exhausted}` per checklist listing the
     exhausted item ids (§6.4's singular "escalation item"), and no
     further reminders for capped items.
   - **One reminder per checklist per due tick** covering all outstanding
     items: AR-3 send, capability `chase.reminder`, template
     `T-06r-broker`|`T-06r-client` selected by recipient party role
     (§6.4); action payload lists **only outstanding** items with
     per-item age-days plus received ✓ ids (pinned §3.1). Recipient
     ladder: requester (intimation sender) party; **from reminder 2 the
     insured party joins `to_party_ids`** — AR-3 has no cc parameter;
     true cc lands with item-1 transport (proposed #168).
   - **Per-checklist deferral (§6.4 v1.1):** any inbound `communications`
     row for the claim within 24h of `now` → the whole checklist's due
     reminders defer to `inbound + 48h` (proposed #172); the tick result
     counts them `deferred`.
   - **Suppression (hard):** FSM ∈ {DECLINED, WITHDRAWN, VOID, SETTLED,
     CLOSED} → consumer on `claim.status_changed` cancels open checklists
     (`cancelled` + `chase.cancelled` event); a cancelled checklist never
     reminds. Per-item `snooze_until` skips selection until past.
   - All reminder sends respect AR-3a (non-exempt → in-window or visible
     `queued_window`).
4. **Hard-gate wiring (§6.5):** console actions, ledgered:
   - `POST /chase/items/{item_id}/attest` — physical items only; officer+
     roles; sets `received` (+`received_at`), writes the R-13 input field
     (`logbook_original → salvage.logbook_held=true`,
     `keys_physical → salvage.keys_held=true`, `human` source, one
     append-only version) so the **existing** PACKET-07 settlement guard
     clears R-13 from real attestations (#173). Attestation event carries
     the attesting actor.
   - `POST /chase/items/{item_id}/waive` — reason mandatory (422
     otherwise); **blocking-checklist items require `claims_manager`**
     (403 otherwise); `waived` + `waived_by` + `waiver_reason`; ledgered.
     `police_abstract` auto-waiver stays impossible (R-11 blocked, item 4
     — PACKET-14 posture).
   - `POST /chase/items/{item_id}/snooze` `{until}` — officer pause per
     item (§6.4); ledgered.
   - `GET /chase/claims/{claim_id}` — checklist + item states for the
     PRD-04 S-2 Documents tab (read).
   - R-14 remains `blocked_on_inputs` (#49 — `settlement.payee_party_id`
     is PRD-12): the discharge item records its signal, and the
     settlement edge stays conservatively blocked citing R-14 (#64
     behaviour preserved; asserted verbatim in scenario 4).
5. **Analytics (§6.6, proposed #171):** dialect-portable computed reads
   (materialised views deferred to the PG infra packet) exposed as three
   live dashboard series + CSV via the existing `ops_reads` pattern:
   `chase_doc_type_cycle` (per-doc-type median request→verified minutes),
   `chase_broker_league` (per-requester-domain median first-response
   minutes + mean reminder count to completion),
   `chase_cycle_time` (per-claim chase-attributable minutes: first
   request → checklist complete). Null/empty medians when no data (#79).
   `packs/motor/dashboard.yaml` gains the three rows (live,
   `eat_calendar`).
6. **SLA `doc_item_age` goes live (proposed #164):** start
   `chase.item_requested`, stop `chase.item_received`, warn 3d / breach 7d,
   `send_window` calendar (PRD-00 §0.5 verbatim); **per-item clock keying**
   — the claim_core SLA engine gains an optional definition `key_field`
   (start stores `payload[key_field]` on the clock row; a stop event stops
   only the matching key), mirroring #145's `stop_filter` precedent.
   `escalate_to_role` stays `pending_capture`; breach staff notification
   already flows through the #111 audience map.
7. **Pack data edits, exactly these:** `packs/motor/checklists/items.yaml`
   (new, §1.1); `packs/motor/chase/chase.yaml` (new: cadence steps, cap,
   deferral windows, cc-from-reminder index, reject-reason map, tick
   interval); `packs/motor/intake/intake.yaml` **loses**
   `checklist_doc_types` (#165); `packs/motor/fields.yaml` gains
   `logbook.bank_interest.present` (bool, pii none — #167);
   `packs/motor/autonomy/policies.yaml` gains
   `{id: chase.reminder, max_level: L4, initial_level: L1}` and
   `{id: chase.rerequest, max_level: L4, initial_level: L1}` (maxes
   unstated by PRD-06 — proposed #169; the §6.4 "fast track ≥25 clean,
   ≥96%" equals the default L1→L2 ladder values, stepwise — no
   skip-to-L3); `packs/motor/dashboard.yaml` series rows (§1.5);
   `platform/claim_core/sla/definitions.yaml` `doc_item_age` activation
   (§1.6).

**Out / visibly blocked:** Graph transport, real sends, durable
window-queue release (item 1 / #130/#157); T-05/T-06/T-06r/T-08 bodies
(item 6/#61); R-11 waiver conditions (item 4) — `police_abstract` never
auto-waived; R-14 payee arm (#49 — PRD-12); `expired`/`wrong_document`
reject reasons (no producing signal — #162); `cert_of_incorporation`
auto-add condition (#166); `logbook.bank_interest.present` extraction
`target_path` (doc-schema spec round — #167); PostgreSQL materialised
views (#171); console S-2/S-4 SPA rendering (PRD-04 scope — this packet
ships the reads); broker league beyond requester-domain grouping
(party-to-broker entity resolution is uncaptured); salvage/PRD-11 lot
mechanics.

## 2. Binding spec quotes (implement verbatim)

PRD-06 §6.2:

> "every item is a registered entity {id, kind: document|physical|
> field_request, doc_type?, target_path?, physical: bool} ... Checklist
> instantiation VALIDATES against this registry — the 422
> FIELD_NOT_IN_DICTIONARY path is closed by design."

> "Every state change emits `chase.item_*` events + timestamps — **this
> table is the cycle-time evidence base** (P-6); rows are never deleted."

PRD-06 §6.3:

> "A classified doc matching **no** outstanding item attaches to the claim
> with timeline note (never discarded). Rejection auto-composes a
> re-request naming the specific defect ... capability `chase.rerequest`,
> launch L1."

PRD-06 §6.4:

> "Cadence (pack config): T+3d, T+7d, T+12d, then every 7d, **cap 6
> reminders** → escalation item to officer (`EXCEPTION{type:
> chase_exhausted}`). Recipient ladder: requester party first (broker if
> broker-intimated — matches current practice), cc insured from reminder
> 2, officer alert at breach (SLA `doc_item_age`, PRD-00)."

> "FSM ∈ {DECLINED, **WITHDRAWN, VOID**, SETTLED, CLOSED} → checklist
> auto-cancelled ... **deferral is per checklist (v1.1):** any inbound
> reply on the claim thread within 24h defers **all of that checklist's**
> reminders 48h"

PRD-06 §6.5:

> "Physical items are human-attested: officer marks received in console
> (S-2 Documents tab), attestation ledgered. Checklist incomplete ⇒
> `claims.blocked_reasons` includes `R-13`/`R-14` ⇒ PRD-12 settlement
> structurally cannot start ... Waivers on blocking items require
> `claims_manager` role, reason mandatory, ledgered."

PRD-06 §6.7:

> "(1) Replay corpus claim: 7-item checklist instantiated, 3 docs arrive
> across 3 emails → matched/verified, reminder at T+3d lists only the
> outstanding 4; (2) illegible logbook → rejected + defect-specific
> re-request drafted; (3) claim declined mid-chase → zero further
> reminders (assert on outbound log); (4) surrender checklist with bank
> interest → discharge-letter item auto-present; settlement transition
> attempted → blocked with `R-13/R-14` reasons surfaced; (5) reminder cap
> → escalation item; (6) analytics view returns non-null medians on
> seeded history."

## 3. Deliverable

```text
agents/chase_agent/
  __init__.py     # build_chase_agent(app, *, config=None)
  checklist.py    # chase.init + surrender instantiation; initial T-06 request
  matcher.py      # §6.3 matching, verify/reject, re-request drafts
  reminders.py    # tick(); cadence, ladder, deferral, suppression, cap
  api.py          # attest / waive / snooze / checklist read
packs/motor/checklists/items.yaml
packs/motor/chase/chase.yaml
platform/claim_core/alembic/versions/0011_chase_checklists.py
docs/runbooks/chase_agent.md
```

**Authorised existing-package changes, exactly these:**

1. `claim_core`: (a) SLA engine `key_field` per-item clock keying (#164) +
   the `doc_item_age` activation in `sla/definitions.yaml`; (b) **one**
   curated method linking an inbound `communications` row to a claim
   (#159 — the router records classified `new_intimation` mail with
   `claim_id NULL`; without linkage, chase replies cannot thread-match);
   (c) `ledger.ACTION_MAP` gains `chase.item_requested`,
   `chase.item_received`, `chase.item_verified`, `chase.item_rejected`,
   `chase.item_waived`, `chase.item_snoozed`, `chase.reminder_sent`,
   `chase.complete`, `chase.cancelled` (#170; precedent #128/#109);
   (d) migration `0011_chase_checklists`. Nothing else.
2. `intake_agent`: the S1/S2 flow calls the #159 linkage for the
   originating message; the S7 checklist step reads `items.yaml` instead
   of `intake.yaml checklist_doc_types` (#165). No other intake change;
   all PACKET-14 suites keep passing unmodified.
3. `review_queue`: the three `ops_reads` series readers + CSV (§1.5
   pattern). No contract change (`chase_exhausted` is an `EXCEPTION`
   subtype under the existing contract).
4. No `eval_harness` change; no `grader_map.yaml` change (chase sends are
   G-COMM-governed communications — the existing critical grader covers
   them); G-PROC stays pending.

`.github/`, `tools/ci/`, protected acceptance files untouched by the
builder. No new CI legs.

### 3.1 Pinned public surface (acceptance relies on exactly this)

- `from chase_agent import build_chase_agent`;
  `build_chase_agent(app, config=None)` after `build_intake_agent` —
  registers dispatcher consumers `chase_checklist` (on `chase.init` +
  `claim.status_changed`) and `chase_matcher` (on `document.classified` +
  `document.extracted`), stores the handle at `app.state.chase_agent`.
  `config` dict override mirrors the intake pattern (None loads
  `packs/motor/chase/chase.yaml`).
- `app.state.chase_agent.tick(now=None) -> {"sent": int, "deferred": int,
  "escalated": int, "suppressed": int}` — `sent` counts staged/executed
  reminder attempts this tick.
- Reminder/re-request/request action payloads (visible in the staged
  `DRAFT_RELEASE` review payload under `action.payload`):
  `template_id`, `outstanding: [{"item_id", "age_days"}, …]` (only
  outstanding), `received: [item_id, …]`, and for re-requests
  `defect: {"item_id", "reason"}`.
- `chase_checklists` / `chase_items` columns queryable exactly per §6.2
  DDL + `snooze_until` (#161).
- Routes: `POST /chase/items/{item_id}/attest` (physical only; 422 on
  non-physical), `POST /chase/items/{item_id}/waive` (`{"reason": str}`
  mandatory → 422; blocking-checklist items non-CM → 403),
  `POST /chase/items/{item_id}/snooze` (`{"until": iso8601}`),
  `GET /chase/claims/{claim_id}` → `{"checklists": [{"id", "purpose",
  "status", "blocking", "items": [{"item_id", "state", …}]}]}`. All
  require `X-Actor` with a mapped role.
- Attestation writes: `logbook_original → salvage.logbook_held`,
  `keys_physical → salvage.keys_held` (`human` source, value `true`).
- Events: `chase.item_requested` / `chase.item_received` /
  `chase.item_verified` / `chase.item_rejected` / `chase.item_waived` /
  `chase.item_snoozed` / `chase.complete` / `chase.cancelled`, each with
  `{"claim_id", "checklist_id", "chase_item_id", "item_id", …}` payload
  cores. **`chase.reminder_sent` fires only on a real transport send**
  (item 1) — staged reminders are visible as the `DRAFT_RELEASE` +
  `reminder_count`, never a fabricated sent event (#108/#157 posture).
- `chase.yaml` keys (config, not code): `version: 1`,
  `cadence_days: [3, 7, 12]`, `repeat_days: 7`, `reminder_cap: 6`,
  `inbound_defer: {window_hours: 24, defer_hours: 48}`,
  `cc_insured_from_reminder: 2`, `reject_reasons` map.
- `sla/definitions.yaml` `doc_item_age` row: `status: live`,
  `start_event: chase.item_requested`, `stop_event: chase.item_received`,
  `key_field: chase_item_id`, `escalate_to_role: pending_capture`.
- Dashboard: `GET /portfolio` gains the three series ids of §1.5;
  `chase_doc_type_cycle` data =
  `{"<doc_type>": {"median_minutes": number, "count": int}, …}`;
  CSV endpoints return 200.
- `items.yaml`: top-level `version: 1`, `items:` mapping keyed by item id
  with `{kind, doc_type?, target_path?, physical}` — ids exactly as §1.1.

## 4. CTO decisions (D-x) and proposed register entries

Builder appends with the implementation PR; entries are **#158–#173**
(#131 remains an unassigned gap — do not reuse).

- **#158 — package naming** (mirror #119): the agent is
  `agents/chase_agent/`, import `chase_agent`; tables on `claim_core.Base`
  with the Alembic/initialise split used for `agent_runs`.
- **#159 — intimation thread continuity.** The §5.2 router records
  classified `new_intimation` mail with `claim_id NULL`; PRD-06 §6.3/§5.2
  require chase replies to thread-match. One curated claim_core method
  links the originating communication row to the S1-created claim; the
  intake flow calls it. Without this, every chase reply falls to
  reference-match or mailbox triage.
- **#160 — the first T-06 request has no named sender in PRD-06.** Staged
  by the chase instantiation consumer at capability `intake.doc_request`
  (PRD-05 §5.6 "pairs with acknowledge"); `requested_at` = gate outcome
  time (staged counts — launch levels stage everything); cadence anchors
  to it.
- **#161 — `snooze_until` named by §6.4, absent from the §6.2 DDL.**
  Column added; register entry is the DDL-addition record.
- **#162 — verification/rejection machine reasons.** "Passing validators"
  realised narrowly as: ≥1 committed field for the linked document →
  `verified`; zero → `rejected{illegible}`; CC-1 flag on that document →
  `rejected{wrong_vehicle}` (#75 limits apply); `expired` /
  `wrong_document` are reserved enum members with no producing signal —
  the map is pack data, never guessed in code.
- **#163 — re-request template unnamed by §6.3.** T-06r tone variant with
  a structured defect payload; bodies `pending_capture` (item 6/#61) →
  visible pending drafts; prose never invented.
- **#164 — `doc_item_age` per-item clocks.** SLA definitions gain an
  optional `key_field`; start stores the event's key, stop stops only the
  matching key (mirror #145). Start `chase.item_requested`, stop
  `chase.item_received`; `escalate_to_role` stays `pending_capture`
  (#111 map already routes SLA staff notifications).
- **#165 — one doc-type registry.** `items.yaml` (PRD-06's registry)
  supersedes PACKET-14's `intake.yaml checklist_doc_types` (#155); intake
  S7 reads `items.yaml`; the duplicate key is removed.
- **#166 — `cert_of_incorporation?` auto-add condition uncaptured.**
  Registered in `items.yaml`; not auto-instantiated; capture wanted
  (corporate-insured signal).
- **#167 — `logbook.bank_interest.present` has no committed path.** The
  PRD-01 logbook schema extracts `bank_interest` without a `target_path`
  and field lists are closed; the pack field is registered now with
  launch source `human` (officer keys it); the extraction `target_path`
  needs a doc-schema spec round.
- **#168 — "cc insured" without a cc channel.** AR-3 `send` has no cc
  parameter; from reminder 2 the insured party joins `to_party_ids`; true
  cc semantics land with the item-1 transport packet.
- **#169 — chase capability ceilings unstated.** `chase.reminder` and
  `chase.rerequest` max L4, initial L1; §6.4's fast-track numbers (≥25
  clean, ≥96%) equal the default L1→L2 ladder — promotion stays stepwise,
  no L1→L3 skip; capture confirmation wanted.
- **#170 — event catalog + ACTION_MAP additions** for the §6.2 "every
  state change" rule (mirror #121/#147): `chase.item_verified`,
  `chase.item_rejected`, `chase.item_waived`, `chase.item_snoozed`,
  `chase.cancelled` join the catalog; the §3-listed ACTION_MAP entries
  keep attest/waive/snooze/reminders ledgered via the single writer.
- **#171 — §6.6 "materialized views" before PG-backed CI.** Dialect-
  portable computed reads with the same data contract; PG materialised
  views + refresh land with the infra packet; empty datasets return
  null/zero (#79).
- **#172 — deferral signal pinned.** "Inbound reply on the claim thread
  within 24h" = newest `direction='inbound'` communications row for the
  claim within 24h of the tick; due reminders defer to `inbound + 48h`,
  whole checklist.
- **#173 — surrender gate bridge.** Trigger = `claim.status_changed` to
  `SURRENDER_CHECKLIST`; attestation writes `salvage.logbook_held` /
  `salvage.keys_held` so the existing R-13 guard clears from real
  attestations; R-14 stays `blocked_on_inputs` (#49) and the settlement
  edge keeps failing closed citing R-14 until PRD-12 supplies the payee
  path.

## 5. Builder guardrails

- **AR-2 is the only door** — request/reminder/re-request all via
  `comms.send`/`execute_or_stage`; zero direct sends; `banned_calls.py`
  green; no funds-transfer anything.
- **Never guess** — unmatched docs attach visibly, never discarded;
  unmappable reject reasons don't exist (the map is closed pack data);
  `police_abstract` never auto-waived (R-11, item 4); R-14 never passes
  while blocked; pending templates render visible pending drafts;
  `cert_of_incorporation` never auto-added.
- **Closed enums** — 17 review types (`chase_exhausted` is an EXCEPTION
  subtype); item states exactly §6.2's six; checklist statuses exactly
  three; five-state FSM suppression set exact.
- **Append-only** — chase rows never deleted; state changes are new
  timestamps + events, `reminder_count` monotonic; field writes
  (attestation, bank-interest) are append-only `human` versions; no
  in-place `claim_fields` updates.
- **Autonomy ceilings** — no widening; `chase.reminder`/`chase.rerequest`
  L1 launch; waiver authority is a **role check**, not a capability.
- **Idempotency** — consumers dedupe on event id; one checklist per
  `chase.init`; one `chase_exhausted` per item; re-delivered
  `document.classified` cannot double-advance state.
- **Determinism** — no RNG; cadence arithmetic on stored timestamps;
  candidate/item ordering by id; tick is pure function of (db, now).
- **Config over code** — cadence, cap, deferral windows, cc index,
  reject-reason map, item registry: pack data. Suppression set and ladder
  *mechanics* are spec logic citing §6.4.
- All PACKET-01–14 suites keep passing unmodified.

## 6. Definition of done (ED-7/ED-7a)

- `tests/acceptance/test_packet_15_chase_agent.py` passes unmodified on
  SQLite and PostgreSQL legs; full suite green.
- §6.7 scenarios (1)–(6) verbatim, including: reminder listing exactly
  the outstanding 4; zero reminders after terminal suppression (asserted
  on the outbound/staged log); cap boundary at exactly 6; blocked
  settlement surfacing both R-13 and R-14; non-null medians on seeded
  history.
- ≥80% coverage on `agents/chase_agent/`; migration `0011` reviewed (two
  tables + `snooze_until`); OpenAPI regenerated for the four chase
  routes; runbook page (reminder triage, attest/waive audit, exhausted
  items, surrender gate); grader coverage unchanged — confirm explicitly
  in the PR description.
- ED-11: further ambiguity ⇒ narrowest safe behaviour + proposed register
  entry (next free number **#174**); stop and flag before expanding this
  packet.
