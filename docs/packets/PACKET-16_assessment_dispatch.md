# PACKET-16 — Assessment orchestration, slice 1: vendors, mode decision, dispatch, assessor SLA (PRD-07 §7.2–§7.4)

> **Status:** issued · **Builder:** Codex per `AGENTS.md` · **Reviewer:** CTO per `CLAUDE.md`
> **Source spec:** `docs/PRD-07_Assessment_Orchestration_Agent_v1.1.md` §7.1–§7.4
> (§7.5–§7.7 cascade/ledger are PACKET-17); Section 0.5 AR-1/AR-2/AR-3/AR-3a;
> PRD-00 §0.4 (FSM edges INTIMATED→…→IN_ASSESSMENT), §0.5 (`assessor_turnaround`);
> PRD-02 R-05/R-06/R-07; PRD-04 §4.3 (closed enum, MODE_CONFIRM);
> PRD-05 §5.2 (thread-match); PRD-06 §6.4 (reminder engine reuse);
> Section 0 ED-8/ED-9/ED-11; guide §3.5/§3.11/§6 (mode_confirm capped L2);
> registers #48/#49/#55/#56/#61/#111/#130/#152/#155/#157/#160/#163/#164/
> #168/#170/#174/#175/#176/#180.
> Precedence: Section 0 → Section 0.5 → PRD-00/PRD-02/PRD-04/PRD-05/PRD-06/PRD-07 →
> PACKET-06..15 contracts → this packet.
> **Depends on:** PACKET-15 merged on main (`a960277`, registers #158–#180).
> **Acceptance:** `tests/acceptance/test_packet_16_assessment_dispatch.py` —
> protected, failing by design until this packet is built.
> **Packet 17 (next):** PRD-07 §7.5–§7.7 — report ingestion, cascade C1–C5,
> `savings_ledger` + FX-1, comparison artifact, WRITE_OFF gate.

## 0. Slice boundary

PACKET-15 ends with the estimate document chased, matched, and
`assessment.estimate_total` committed. This packet builds the outbound half
of PRD-07 on the existing runtime: the vendor registry, the estimate-verified
trigger and FSM advance to `IN_ASSESSMENT`, the dual-path mode decision
(Path A rule R-06 + officer `MODE_CONFIRM`; Path B permanently-L0 shadow
model), assessor dispatch (T-11, cc broker, doc-pack attachments,
multi-assessor N-send), the `assessor_turnaround` SLA activation, and the
warn-day assessor reminder via the PACKET-15 chase engine (reuse, don't
rebuild — PRD-07 §7.4 verbatim).

The seam is the assessor's reply: report ingestion, the C1–C5 cascade,
`savings_ledger`, and every §7.7 scenario that consumes a parsed report are
**PACKET-17**. This packet's `assessment.report_received` stop event is
registered but nothing fires it yet.

**Still no live Graph** (open item 1): T-11 is `pending_capture` (item 6 /
#61 — §7.4's "interim: reconstructed from observed emails" needs the capture
sitting; no body is invented from thin air, proposed #189) and the AR-3
transport is `pending_capture`. Every dispatch and assessor reminder
terminates in a visible staged draft / pending-template draft (#130/#157
posture unchanged). R-06 stays `blocked_on_inputs` (Q-02): the mode item
goes to the officer **undetermined** and their choice is labelled training
data (§7.3 Path A verbatim).

## 1. Scope

**In:**

1. **`agents/assessment_agent/` (import `assessment_agent`, proposed #181 —
   mirrors #119/#158).** Built by
   `build_assessment_agent(app, *, model_client, config=None)` after
   `build_chase_agent`. Idempotent event consumers + gate-governed actions;
   every governed action's durable record is the gate's own `agent_runs` row.
   - **`vendors` table (§7.2 DDL verbatim):** `vendors(id, kind:
     assessor|garage|supplier|salvage_yard, name, emails[], fee_schedule
     JSONB, active)`. On `claim_core.Base`; migration
     `0012_vendors_assessment`; rows never deleted (ED-9 — deactivate via
     `active=false`). Seed registry `packs/motor/vendors/vendors.yaml`
     (new): standard fee schedule as data —
     `{physical: 638_000, desk: 0, reinspection: 290_000}` (cents; PRD-07
     §7.2 observed KES 6,380 / 0 / 2,900). Real assessor-firm rows are
     `pending_capture` (embed seeding); the §7.2 ❓ per-firm fee schedule
     stays open (proposed #186). `config["vendors"]` override mirrors the
     intake config pattern; seeding is an idempotent upsert by id.
     Auto-rotation is **out of scope until data exists** (§7.2 verbatim) —
     no selection heuristic anywhere.
   - **Read route `GET /vendors?kind=`** — active vendors for the console
     picker; `X-Actor` with a mapped role required.
2. **Estimate-verified trigger (§7.3, proposed #182):** consumer on
   `field.updated` for path `assessment.estimate_total` with a committed
   verification state (default floor per #56). On first qualifying commit:
   - FSM advance, stepwise and event-per-hop: `TRIAGED → AWAITING_DOCS →
     IN_ASSESSMENT` (the `AWAITING_DOCS → IN_ASSESSMENT` edge is the
     PRD-00 "estimate received" guard edge; no PRD assigns the
     `TRIAGED → AWAITING_DOCS` hop — narrowest owner is this trigger,
     proposed #182). From `AWAITING_DOCS`, one hop. Already
     `IN_ASSESSMENT`+: no transition. Suppressed/terminal states
     (`suppresses_activity`): no action, mirror of chase suppression.
     Any other state: `EXCEPTION{assessment_out_of_sequence}` with the
     four-part contract — never a silent skip.
   - **Path A:** evaluate R-06 via the existing cop_runtime.
     `blocked_on_inputs` (Q-02) → verdict `undetermined`, rule_run id
     recorded. Then stage the mode item as a governed
     `assessment.mode_confirm` action (gate.yaml already maps it to
     **MODE_CONFIRM**): payload carries estimate total, the R-06 verdict
     (`blocked_on_inputs` visible) + `rule_run_id`, the photos strip
     (`photo_damage` document ids), and the estimate document id. Ceiling
     stays **max L2 per guide §6** — PRD-07's "max L3" applies only after
     Q-02 lands and is a pack change gated on capture + owner sign-off
     (proposed #185). Launch level: registered initial (L1 default) — the
     card is always staged at launch.
   - **Path B (shadow, L0 permanently until re-decided, §7.3 verbatim):**
     one `MODEL_HEAVY` `structured_call` task `assessment_mode_shadow`,
     inputs {estimate total + line-items document id, damage-photo document
     ids, `loss.narrative`, vehicle age} → `{mode, rationale, confidence}`.
     Vehicle age has **no registered field path** — the input is recorded
     `null`, never derived (proposed #192). Logged to `agent_runs` at
     capability `assessment.mode_shadow` (max **L0**, proposed #192),
     `rationale` redacted in audit payloads (doc_intel
     `audit_redacted_keys` posture). **Never surfaced:** zero review items,
     zero events, zero console fields from this path. Weekly Path-B-vs-
     officer comparison needs the PRD-03 corpus machinery — the packet
     ships the labelled pairs only (proposed #193, mirror #36/#55).
   - Idempotency: one open MODE_CONFIRM per claim; re-delivered events and
     further estimate versions while the card is open are no-ops; a new
     committed estimate version **after** a mode decision opens a fresh
     card (officer re-confirms; proposed #182). Rejecting the card
     idempotently re-issues a fresh card linked by `retry_of` (mirror
     #152). One shadow run per issued card.
3. **Mode resolution → dispatch (§7.2/§7.4):** resolution schema
   **`MODE_CONFIRM@2`** (proposed #183 — @1 carries no decision content;
   @1 stays registered for replay): `{capability_id, diff, decision:
   {mode: desk|physical, vendor_ids: [≥1 registry ids]}}`. §7.2's "officer
   picks from registry in the dispatch confirm item" rides this card
   (proposed #188). Unknown/inactive vendor id → 422, card stays open.
   On approve, in one governed flow:
   - Writes `assessment.mode` (new pack enum field desk|physical) and,
     when `len(vendor_ids) > 1`, `assessment.multi_mode=true` (§7.4 R-07
     multi-assessor mode) — append-only `human`-source versions
     (officer's resolution), proposed #184. R-07 itself stays
     `blocked_on_inputs` (#49) — selection semantics are PACKET-17.
   - **Vendor→party bridge (proposed #187):** AR-3 sends to parties; per
     selected vendor, idempotently create/reuse a claim-scoped party
     `role='assessor'`, `email=vendors.emails[0]`, `meta.vendor_id`.
   - **Per-vendor T-11 dispatch** staged via AR-3 at capability
     **`assessment.dispatch`** (new: max L3, initial L1 — §7.4 "Launch
     L1 → L2; L3 after 25 clean" = the default stepwise ladder):
     `to_party_ids = [assessor_party, broker_party]` (**cc broker** §7.4;
     AR-3 has no cc — broker joins recipients, mirror #168). Action
     payload (via the #175 mapping): `template_id: T-11`, `vendor_id`,
     `mode`, `merge` inputs {claim ref, insured, reg, loss summary, mode,
     garage details} with unresolvable inputs listed under `missing`
     (garage details have no committed source pre-repair — visible gap,
     never invented), `attachments` = held document ids of the §7.4
     doc-pack subset {claim_form, repair_estimate, photo_damage, logbook}
     with absent members listed under `missing_attachments`. T-11 body
     `pending_capture` → visible pending-template draft; `email.sent`
     never fires here (#157).
   - `assessment.dispatched` event per vendor at the gate outcome
     (**staged counts**, mirror #160) — this starts the SLA clock.
4. **SLA `assessor_turnaround` goes live (§7.4, proposed #191):**
   `start_event: assessment.dispatched`, `stop_event:
   assessment.report_received` (registered; fired by PACKET-17),
   `key_field: assessor_party_id` (per-firm clocks in multi-assessor mode,
   mirror #164), warn 3d / breach 5d (PRD verbatim), `calendar: business`,
   `escalate_to_role` stays `pending_capture`; breach staff notification
   flows through the existing #111 audience map.
5. **Warn-day assessor reminder via chase engine (§7.4 "reuse, don't
   rebuild", proposed #190):** per dispatched vendor, one one-item
   checklist `purpose='assessor_report'` (item `assessor_report`, kind
   document, doc_type `assessor_report`), `requester_party_id` = that
   assessor party. Chase changes, exactly these: `chase_checklists.purpose`
   CHECK widens to `('claim_docs','surrender','assessor_report')` and the
   table gains nullable `requester_party_id` (null = existing
   intimation-sender selection, #176 unchanged) — both in migration 0012;
   reminder tone for assessor-requester checklists is new registry row
   **`T-06r-assessor`** (`pending_capture`, mirror #163).
   Cadence/cap/deferral/suppression: the existing chase.yaml values
   unchanged (first step T+3d = the warn day). The `items.yaml`
   `assessor_report` row and the matching PACKET-15 protected-constant
   amendment ship **owner-side in the spec commit** (precedent #174), so
   the PACKET-15 registry pin stays exact and green — the builder touches
   neither protected files nor `items.yaml`.
6. **Pack/platform data edits, exactly these:**
   `packs/motor/vendors/vendors.yaml` (new, §1.1);
   `packs/motor/fields.yaml` gains `assessment.mode` (enum desk|physical)
   and `assessment.multi_mode` (bool), both `pii_class: none` (#184);
   `packs/motor/autonomy/policies.yaml` gains
   `{id: assessment.dispatch, max_level: L3, initial_level: L1}` and
   `{id: assessment.mode_shadow, max_level: L0, initial_level: L0}`
   (`assessment.mode_confirm` row **unchanged** — max L2, #185);
   `packs/motor/review/schemas/MODE_CONFIRM@2.json` + the
   `contracts.yaml` MODE_CONFIRM `resolution_schema` bump;
   `packs/motor/templates/registry.yaml` gains `T-06r-assessor`
   (`pending_capture`, #190); `platform/claim_core/sla/definitions.yaml`
   `assessor_turnaround` activation (§1.4).

**Out / visibly blocked:** report ingestion, thread-matched assessor
replies, cascade C1–C5, comparison artifact, `savings_ledger`, FX-1, MTD
tile, WRITE_OFF transition, R-07 selection semantics + quote paths (#49 —
all PACKET-17); Graph transport / real sends (item 1, #130/#157); T-11 and
T-06r-assessor bodies (item 6/#61/#189); R-06 threshold (Q-02, item 5) —
mode item always undetermined; per-firm fee schedules + real firm seeds
(§7.2 ❓, #186); assessor auto-rotation (§7.2 — no data); vehicle-age field
registration (#192); weekly Path-B agreement eval (#193 — PRD-03 corpus
machinery); `icon.assessor_payment_request` fee ops (PRD-09/12 scope);
mode_confirm L3 auto-apply (#185 — needs Q-02 + 50 clean confirms +
pack change).

## 2. Binding spec quotes (implement verbatim)

PRD-07 §7.2:

> "`vendors(id, kind: assessor|garage|supplier|salvage_yard, name, emails[],
> fee_schedule JSONB, active)` — seeded at embed: assessor firms + standard
> fees (physical KES 6,380 observed; desk 0; re-inspection 2,900 ❓confirm
> distinct fee schedule per firm). Assessor selection v1 = officer picks
> from registry in the dispatch confirm item; auto-rotation is out of scope
> until data exists."

PRD-07 §7.3:

> "**Path A (authoritative):** rule R-06 threshold compare → recommendation.
> Until Q-02 lands, rule sits `blocked_on_inputs` and the mode item goes to
> the officer undetermined (they choose; their choice is _labelled training
> data_)."

> "**Path B (shadow, L0 permanently until re-decided):** `MODEL_HEAVY` with
> inputs {estimate total + line items, damage photos, narrative, vehicle
> age} → `{mode, rationale, confidence}`. Logged to `agent_runs`, **never
> surfaced**."

> "Officer confirms via `MODE_CONFIRM` review item (shows estimate,
> threshold verdict, photos strip)."

PRD-07 §7.4:

> "Template `T-11` (❓capture verbatim at embed ...) — merge: claim ref,
> insured, reg, loss summary, mode, garage details; **cc broker** (current
> practice, keep); attachments = doc-pack subset {claim form, estimate,
> photos, logbook} assembled from S3. Multi-assessor mode (R-07): officer
> may select N firms in the confirm item → N sends,
> `assessment.multi_mode=true`. Launch L1 → L2; L3 after 25 clean."

> "SLA `assessor_turnaround` starts at send (warn 3d, breach 5d); reminder
> to assessor at warn via chase engine (a one-item checklist
> `purpose='assessor_report'` — reuse, don't rebuild)."

Guide §6 (precedence over PRD-07 §7.3's L3):

> "R-06 desk/physical threshold | rule `blocked_on_inputs`; MODE_CONFIRM
> goes to officer undetermined (their choices are labelled training data);
> `assessment.mode_confirm` capped L2 | open item 5"

## 3. Deliverable

```text
agents/assessment_agent/
  __init__.py     # build_assessment_agent(app, *, model_client, config=None)
  vendors.py      # vendors model/seed + GET /vendors
  trigger.py      # estimate-verified consumer; FSM advance; Path A + shadow
  dispatch.py     # MODE_CONFIRM@2 resolution → parties, T-11 sends, checklists
packs/motor/vendors/vendors.yaml
packs/motor/review/schemas/MODE_CONFIRM@2.json
platform/claim_core/alembic/versions/0012_vendors_assessment.py
docs/runbooks/assessment_agent.md
```

**Authorised existing-package changes, exactly these:**

1. `claim_core`: (a) `sla/definitions.yaml` `assessor_turnaround`
   activation (§1.4); (b) `ledger.ACTION_MAP` gains
   `assessment.mode_item_created`, `assessment.mode_decided`,
   `assessment.dispatched` (#194; precedent #170); (c) migration
   `0012_vendors_assessment` (vendors table + the two chase columns/CHECK).
   Nothing else.
2. `chase_agent`: `purpose='assessor_report'` accepted; instantiation from
   the pinned §3.1 method; requester selection honours
   `requester_party_id` when set (falls back to #176 marker selection);
   tone `assessor` → `T-06r-assessor`. All PACKET-15 suites keep passing
   against the owner-amended fixture; no other chase behaviour change.
3. `review_queue` / packs: MODE_CONFIRM `resolution_schema` →
   `MODE_CONFIRM@2` (contract's versioned-schema mechanism; @1 file
   remains). No new review type — the enum stays 17.
4. No `eval_harness` change: dispatch/reminder sends are G-COMM-governed
   communications; the shadow path is L0 log-only and its grading is #193
   (PRD-03 corpus scope, mirror PACKET-15 posture).

`.github/`, `tools/ci/` untouched by the builder; protected acceptance
files untouched by the builder (the PACKET-15 constant amendment is
owner-committed with this spec). No new CI legs.

### 3.1 Pinned public surface (acceptance relies on exactly this)

- `from assessment_agent import build_assessment_agent`;
  `build_assessment_agent(app, model_client=…, config=None)` after
  `build_chase_agent` — registers consumers on `field.updated` and stores
  the handle at `app.state.assessment_agent`. `config` override mirrors
  intake (`None` loads `packs/motor/vendors/vendors.yaml`); test seeding
  via `config={"vendors": [{id, kind, name, emails, fee_schedule,
  active}, …]}`.
- `vendors` columns queryable exactly per §7.2 DDL. `GET /vendors?kind=`
  → `{"vendors": [{"id", "kind", "name", "emails", "fee_schedule",
  "active"}]}`, active only, `X-Actor` + mapped role required.
- MODE_CONFIRM staged card `payload["action"]["payload"]`:
  `{"estimate_total": int_cents, "rule": {"rule_id": "R-06", "status":
  "blocked_on_inputs", "rule_run_id": str, "verdict": "undetermined"},
  "photos": [document_id, …], "estimate_document_id": str}`.
- Shadow: exactly one `structured_call(tier="MODEL_HEAVY", …)` with
  `inputs["task"] == "assessment_mode_shadow"` per issued card;
  `inputs["vehicle_age"] is None`; one `agent_runs` row with
  `capability_id="assessment.mode_shadow"`, `level="L0"`; **no** review
  item, event, or claim-read surface carries the shadow output.
- `MODE_CONFIRM@2` resolution payload:
  `{"capability_id": "assessment.mode_confirm", "diff": …, "decision":
  {"mode": "desk"|"physical", "vendor_ids": [str, ≥1]}}`. Unknown or
  inactive vendor id → 422 `VENDOR_NOT_REGISTERED`, card stays open.
  Reject → fresh open MODE_CONFIRM with `payload["retry_of"]` = the
  rejected item id.
- On approve: committed fields `assessment.mode` (`human`) and — iff
  multi — `assessment.multi_mode=true`; one claim-scoped party per vendor
  (`role='assessor'`, `meta.vendor_id`); per vendor one `DRAFT_RELEASE`
  with `payload["capability_id"]=="assessment.dispatch"` and
  `action.payload` `{"template_id": "T-11", "vendor_id", "mode",
  "merge": {"claim_ref", "insured_name", "vehicle_reg", "loss_summary",
  "mode"}, "missing": [merge-input paths, e.g. garage details],
  "attachments": [held doc ids], "missing_attachments": [item names]}`;
  recipients `to_party_ids` = [assessor party, broker party]; one
  `assessment.dispatched` event per vendor (payload carries `vendor_id`,
  `assessor_party_id`); `email.sent` never fires.
- Per dispatched vendor: one `chase_checklists` row
  `purpose='assessor_report'`, `requester_party_id` = assessor party,
  one `assessor_report` item in `requested`; `sla_clocks` row
  `definition_id='assessor_turnaround'`, running, one per assessor party.
- Chase `tick()` at T+3d stages the assessor reminder as `DRAFT_RELEASE`
  capability `chase.reminder`, `action.payload["template_id"] ==
  "T-06r-assessor"`, recipient the assessor party.
- Events registered + ledgered (#194): `assessment.mode_item_created`,
  `assessment.mode_decided` (`{"claim_id", "mode", "vendor_ids"}` core),
  `assessment.dispatched`. `assessment.report_received` is registered as
  the SLA stop event only — nothing in this packet fires it.
- `sla/definitions.yaml` `assessor_turnaround` row: `status: live`,
  `start_event: assessment.dispatched`, `stop_event:
  assessment.report_received`, `key_field: assessor_party_id`,
  `warn_after: 3d`, `breach_after: 5d`, `calendar: business`,
  `escalate_to_role: pending_capture`.
- `vendors.yaml`: top-level `version: 1`, `standard_fees:
  {physical: 638000, desk: 0, reinspection: 290000}`, `vendors: []`
  with a `pending_capture` marker for the embed seed (#186).
- FSM: claim reads `IN_ASSESSMENT` after the trigger;
  `claim.status_changed` events exist for each hop taken.

## 4. CTO decisions (D-x) and proposed register entries

Builder appends with the implementation PR; entries are **#181–#194**.

- **#181 — package naming** (mirror #158): `agents/assessment_agent/`,
  import `assessment_agent`; tables on `claim_core.Base`; migration
  `0012_vendors_assessment`.
- **#182 — estimate trigger + FSM hop ownership.** "On
  `assessment.estimate_total` verified" realised as the `field.updated`
  consumer with the #56 default verification floor. No PRD assigns the
  `TRIAGED → AWAITING_DOCS` hop: this trigger owns it, stepwise with an
  event per hop. Suppressed states → no-op; unexpected states →
  `EXCEPTION{assessment_out_of_sequence}`. One open card per claim;
  estimate versions while a card is open are no-ops; a post-decision
  version opens a fresh card — never silent.
- **#183 — MODE_CONFIRM@1 carries no decision.** `MODE_CONFIRM@2` adds
  `decision{mode, vendor_ids}`; @1 stays registered for replay of
  historical resolutions (versioned-schema contract, PRD-04).
- **#184 — `assessment.mode` / `assessment.multi_mode` paths unnamed.**
  PRD-07 names `assessment.multi_mode=true` but neither path is in any
  dictionary; both registered as pack fields, written append-only
  `human`-source from the officer's resolution.
- **#185 — mode_confirm ceiling conflict.** PRD-07 §7.3 says max L3;
  guide §6 pins "capped L2" while R-06 is blocked. Guide precedence wins:
  pack row stays `max_level: L2`. Raising to L3 (auto-apply + 10%
  sampling) requires Q-02 capture, 50 clean confirms, a pack change, and
  owner sign-off.
- **#186 — vendor seed uncaptured.** Standard fees ship as pack data
  (cents); the real firm list and the §7.2 ❓ per-firm fee schedule await
  the embed capture; tests seed synthetic vendors via config.
- **#187 — vendor→party bridge.** AR-3 addresses parties, vendors are not
  parties: dispatch idempotently creates/reuses one claim-scoped
  `role='assessor'` party per selected vendor (`emails[0]`,
  `meta.vendor_id`).
- **#188 — dispatch confirm surface.** §7.2's vendor pick rides
  MODE_CONFIRM@2; each send then stages as `DRAFT_RELEASE` at
  `assessment.dispatch` L1 — two human touches at launch, collapsing per
  the normal ladder at L2+.
- **#189 — T-11 "interim reconstruction" is not in the repo.** No observed
  emails are checked in; the registry row stays `pending_capture` and
  dispatch drafts render the pending-template posture (#157). The
  reconstruction happens at the item-6 capture sitting, not in code.
- **#190 — assessor_report chase reuse.** `purpose` CHECK widened;
  nullable `chase_checklists.requester_party_id` added (null = #176
  marker selection); the `items.yaml` `assessor_report` row and the
  matching PACKET-15 protected-constant amendment shipped owner-side in
  the spec commit (precedent #174); reminder tone row `T-06r-assessor`
  registered `pending_capture` (mirror #163).
- **#191 — `assessor_turnaround` activation.** Start
  `assessment.dispatched` (staged counts, mirror #160), stop
  `assessment.report_received` (PACKET-17 fires it), `key_field:
  assessor_party_id` (mirror #164), business calendar, warn 3d/breach 5d;
  `escalate_to_role` stays `pending_capture` (#111 map routes staff
  notification).
- **#192 — shadow path gaps.** Vehicle age has no registered field: the
  shadow input records `null`, never a derived value. Capability
  `assessment.mode_shadow` max L0 **permanently** (§7.3) — a max_level no
  promotion can pass; `rationale` joins the audit-redaction set.
- **#193 — weekly Path-B agreement eval deferred.** Needs the ≥100-claim
  corpus (item 7) and PRD-03 measurement machinery (mirror #36/#55); this
  packet ships the labelled pairs (shadow `agent_runs` rows + officer
  `assessment.mode_decided` outcomes). Do not treat packet-16 green as the
  §7.3 agreement-gate discharge.
- **#194 — event catalog + ACTION_MAP additions** (mirror #170):
  `assessment.mode_item_created`, `assessment.mode_decided`,
  `assessment.dispatched`, and the registered-but-unfired
  `assessment.report_received`; ledger stays single-writer.

## 5. Builder guardrails

- **AR-2 is the only door** — dispatch sends and assessor reminders via
  `comms.send`/`execute_or_stage`; zero direct sends; `banned_calls.py`
  green; **no funds-transfer op enters any registry** (PRD-12 acc. 6 —
  assessor *fee schedules* are data, fee *payment* is not built).
- **Never guess** — mode verdict stays `undetermined` while R-06 is
  blocked; vehicle age `null`, never derived; garage merge details listed
  `missing`, never invented; absent doc-pack attachments listed, never
  fabricated; T-11/T-06r-assessor prose never invented; no assessor
  auto-rotation or default vendor.
- **Shadow stays dark** — Path B output must be unreachable from every
  API/console/read surface and every review-item payload; L0 forever;
  UI-surface assertion is in the acceptance suite (PRD-07 acc. 4).
- **Closed enums** — review types stay 17 (`MODE_CONFIRM` exists;
  `assessment_out_of_sequence` is an `EXCEPTION` subtype with the
  four-part contract); vendor kinds exactly §7.2's four; mode exactly
  {desk, physical}; chase purposes exactly three after migration.
- **Append-only** — vendors deactivate, never delete; `assessment.mode` /
  `multi_mode` are new versions, never in-place; agents never supersede
  `human_verified` (409).
- **Autonomy ceilings** — mode_confirm max L2 (guide §6); dispatch max L3
  initial L1; shadow max L0; no widening anywhere; approval authority is
  not a capability.
- **Idempotency** — consumers dedupe on event id; one card per claim per
  open window; one shadow run per card; re-approve/re-deliver cannot
  double-dispatch (one draft per (claim, vendor, card)); party bridge
  reuses existing rows.
- **Determinism** — no RNG; vendor and party ordering by id; the trigger
  is a pure function of (db, event).
- **Config over code** — fees, vendor seeds, schema files, template rows,
  SLA thresholds, capability rows: pack data. FSM hop logic and gate
  mechanics are spec logic citing §7.3/§7.4.
- All PACKET-01–15 suites keep passing (15 against the owner-amended
  constant only).

## 6. Definition of done (ED-7/ED-7a)

- `tests/acceptance/test_packet_16_assessment_dispatch.py` passes
  unmodified on SQLite and PostgreSQL legs; full suite green.
- PRD-07 acceptance (4) verbatim (shadow zero-surface) and the §7.7 (1)
  front half (estimate in → mode item → dispatch draft); boundary/negative
  cases: unknown vendor 422, reject re-issue, duplicate-event idempotency,
  suppressed-state no-op.
- ≥80% coverage on `agents/assessment_agent/`; migration `0012` reviewed
  (vendors + two chase columns + CHECK widening); OpenAPI regenerated
  (`GET /vendors`); runbook page (mode-card triage, dispatch drafts,
  assessor reminders, SLA clocks); grader coverage unchanged — confirm
  explicitly in the PR description.
- ED-11: further ambiguity ⇒ narrowest safe behaviour + proposed register
  entry (next free number **#195**); stop and flag before expanding this
  packet.
