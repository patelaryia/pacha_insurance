# PACKET-08 — Eval harness & autonomy controller: data model, deterministic graders, promotion machinery, reporting (PRD-03 slice 1 of 2)

> **Status:** issued · **Builder:** Codex per `AGENTS.md` · **Reviewer:** CTO per `CLAUDE.md`
> **Source spec:** `docs/PRD-03_Eval_Harness_and_Autonomy_Controller_v1.1.md` §3.2–§3.4,
> §3.6, §3.7 (deterministic subset); Section 0.5 AR-2 (consumed, not built); PRD-00
> §0.3 event catalog (`grader.*`, `autonomy.*`); guide §3.11 autonomy constitution.
> Precedence: Section 0 → Section 0.5 → PRD-03 → this packet.
> **Depends on:** PACKET-07 merged on main (`cop_runtime` complete, PR #6).
> **Acceptance tests:** `tests/acceptance/test_packet_08_eval_autonomy.py` —
> protected, failing by design until this packet is built.
> **Packet 9 (next, PRD-03 slice 2):** model graders G-CITE (crop verify) + G-NOTE
> (rubric), correction capture loop (§3.5, consumes PRD-04 `review.resolved`
> production traffic), weekly batch eval + corpus runner (<30 min / 100 cases),
> anonymisation script, seed corpus intake (open item 7).

## 1. Scope

**In:**

1. New package **`platform/eval_harness/`**, import `eval_harness` (register #66).
2. **Data model §3.2 — DDL binding:** `test_cases`, `grader_runs`, `capabilities`,
   `autonomy_changes`, columns and comments exactly as written; one additive
   Alembic migration chained to head.
3. **Grader framework:** registered grader classes `grade(subject) → result`
   (`pass|fail|error`), every grade writes `grader_runs` + emits
   `grader.passed`/`grader.failed`; event-driven consumer `"eval"` on the
   dispatcher grades production outputs as they occur (`calc.executed`,
   `rule.evaluated`, `template.rendered`); a callable gating seam
   (`grade_output(...)`) for the Phase-2 AR-2 gate.
4. **Deterministic graders live:** `G-VAL`, `G-CALC`, `G-RULE`, `G-SUM`, `G-TPL`
   (§3.3 logic verbatim, severities verbatim). **Registered but pending:**
   `G-CITE`/`G-NOTE` (model calls — slice 2), `G-COMM` (no outbound email exists
   until PRD-06), `G-PROC` (needs `cop_steps.yaml` + agent runs — Phase 2/PRD-13);
   each pending entry carries `blocked_on` (register #67).
5. **Production gating rule (§3.3):** `critical` fail ⇒ `review.created`
   `{type: EXCEPTION, subtype: grader_critical_fail, grader_id, subject_ref}` +
   `grader.failed`; `major` fail ⇒ recorded, allowed at L1/L2, blocks at L3+
   (the block side activates with the Phase-2 gate — the rule is data now).
6. **Autonomy controller (§3.4):** capability registry seeded from
   `packs/motor/autonomy/policies.yaml` (interim pack artifact, PRD-13
   formalises); **hard-coded constitution** (guide §3.11) that pack data may
   tighten but never widen; default promotion policy; **literal consecutive-25**
   L1→L2 with hard reset; rolling-window L2→L3 (50, ≥98%, zero critical) and
   L3→L4 (100, ≥99%, zero critical 60d); material-edit classifier; deterministic
   sampling selector + `SAMPLE_REVIEW` emission helper; promotion API with
   sign-off enforcement (403 without); **GP-1**: promotion of any `settlement.*`
   capability ⇒ `403 GATE_GP1_CLOSED`; **audit-degraded / frozen**:
   `platform_state['autonomy_promotions_frozen']` ⇒ `403 PROMOTIONS_FROZEN`
   (integrates PACKET-03 §4.4); auto-demotion consumer (critical grader failure
   at L3+ ⇒ drop one level within one event cycle + `ops.alert`; rolling-20 pass
   <95% ⇒ drop one level); every change → `autonomy_changes` row with evidence
   snapshot + `autonomy.promoted`/`autonomy.demoted` event + ledger row.
7. **Reporting API (§3.6):** `GET /eval/capabilities`, `GET /eval/corpus/stats`,
   `GET /eval/runs?capability=`, `GET /eval/series` (autonomy rate, no-touch
   rate, accuracy by capability, median review time) — first-class, computed
   from `grader_runs`/`autonomy_changes`/review events.
8. **Grader-coverage guarantee (§3.7):** every live output type
   (`field`, `rule`, `calc`, `artifact`) has ≥1 **live critical** grader —
   enforced by the protected acceptance suite (the ED-7a `grader_map` check).

**Out (packet 9):** G-CITE/G-NOTE implementations; §3.5 correction capture loop
(consumes PRD-04 resolution traffic); weekly batch eval, corpus runner + <30 min
scorecard acceptance; anonymisation script; seed corpus (open item 7, registers
#36/#55). **Out (elsewhere):** AR-2 `execute_or_stage` gate module (ships with
the Phase-2 kickoff packet — it consumes this controller, register #68); PRD-04
console charts and review workspaces; RBAC/role validation on sign-offs (PRD-04;
sign-off actors recorded verbatim now, register #69).

## 2. Binding spec quotes (implement verbatim)

PRD-03 §3.2 — the four CREATE TABLE blocks are DDL-binding (column names, types,
comments). `capabilities.current_level` default `'L1'`.

PRD-03 §3.3, gating:

> "**Production gating rule:** `critical` fail ⇒ output blocked, review item
> created, `grader.failed` emitted. `major` fail ⇒ output allowed at L1/L2
> (human will see it) but blocks at L3+."

PRD-03 §3.4, counters (binding):

> "**L1→L2 is literal consecutive-25:** any material edit or reject **resets the
> counter to zero**. … **L2→L3 and L3→L4 are rolling-window pass rates:** window
> = the stated item count (50 / 100); rejects and material edits both count as
> failures; formatting-only edits count as passes."

PRD-03 §3.4, material edit:

> "any change to a money/date/party/enum field value, or >15% token-level change
> to generated prose. Formatting-only edits don't count."

PRD-03 §3.4, sampling:

> "Selection is **deterministic**: `int(sha256(run_id)[:8], 16) % 100 < rate` —
> reproducible in tests and audits." (`sha256` hex digest; first 8 hex chars.)

PRD-03 §3.4, ceilings:

> "`max_level` ceilings hard-code the constitution: `assessment.consistency_flag`=L2,
> `triage.ex_gratia`=L1, anything touching approval authority = not a capability
> at all."

Guide §3.11 (constitution, hard-coded): `triage.ex_gratia` L1 permanent;
`triage.decline_draft` release = human; CC-5-class flags = L2; `pack.note_draft`
sign = human, max L3; `salvage.award` is **not a capability**; **no L4 anywhere
money-adjacent**; approval authority is not a capability.

PRD-03 §3.7 (this packet's subset):

> "Every PRD-01/02 output type has ≥1 critical grader registered (CI check
> enforces); simulated critical failure at L3 demotes within one event cycle and
> pages; promotion is impossible via API without the sign-off record (403
> otherwise)."

## 3. Deliverable

```
platform/eval_harness/
  __init__.py       # build_eval_harness, EvalHarness, GraderResult, PromotionDenied
  graders.py        # framework + G-VAL/G-CALC/G-RULE/G-SUM/G-TPL + pending slots
  gating.py         # production gating rule + grade_output seam
  autonomy.py       # controller: levels, counters, material edit, sampling,
                    #   promotion/demotion, constitution
  policies.py       # policies.yaml loader + constitution enforcement
  models.py         # four §3.2 tables on claim_core Base (per-dialect JSONB)
  api.py            # §3.6 routes mounted on the claim_core app
packs/motor/autonomy/policies.yaml
```

Cross-package access via `claim_core`/`cop_runtime` root exports and `app.state`
only (ED-1). Authorized `claim_core` change — **exactly one** (register #70):
the ledger consumer's action map gains `autonomy.promoted`/`autonomy.demoted` →
ledger rows (PACKET-03 §4.4 anticipated the map growing). Nothing else.

### 3.1 Pinned surface (acceptance tests rely on exactly this)

```python
from eval_harness import build_eval_harness
harness = build_eval_harness(app)          # requires app.state.cop_runtime
# app.state.eval_harness = harness
# harness.graders.ids()                    -> the nine §3.3 ids
# harness.graders.get(id).severity/.status/.blocked_on
# harness.grade(grader_id, subject_ref: dict, actor) -> GraderResult
#   GraderResult{grader_id, subject_type, result: "pass"|"fail"|"error",
#                severity, detail, grader_run_id}
# harness.autonomy.level(capability_id) -> "L0".."L4"
# harness.autonomy.evidence(capability_id) -> dict   (counters/windows snapshot)
# harness.autonomy.should_sample(run_id: str, rate: int) -> bool   (§3.4 formula)
# harness.autonomy.request_promotion(capability_id, to_level,
#       sign_offs=[{"actor": "user:<ULID>", "role": "claims_manager"|"md"}],
#       actor=...) -> dict | raises PromotionDenied(code=...)
#   codes: SIGN_OFF_REQUIRED | CRITERIA_NOT_MET | GATE_GP1_CLOSED |
#          PROMOTIONS_FROZEN | CEILING_EXCEEDED | UNKNOWN_CAPABILITY
# POST /eval/capabilities/{id}/promote -> 200 | 403 {"code": <same codes>}
# GET /eval/capabilities | /eval/runs?capability= | /eval/corpus/stats | /eval/series
```

Consumers registered on the dispatcher: `"eval"` (grades outputs) and
`"autonomy"` (promotion counters from `review.resolved`, demotion from
`grader.failed`). Both idempotent, synchronously drivable via `dispatch_once()`.

### 3.2 Grader specifics (§3.3 logic verbatim)

- **G-VAL** (`field`, critical): resolve the field's named validator through the
  `doc_intel` schema registry (`target_path` mapping); re-run it against the
  current value. No schema maps the path ⇒ result `error` (visible, never a
  silent pass). Validator outcome `fail`/`out_of_scope` ⇒ `fail`.
- **G-CALC** (`calc`, critical): independent re-execution from **current claim
  inputs** via `cop_runtime.execute_calc` semantics; byte-equal comparison of
  canonical-JSON outputs vs the recorded `calc_runs.output`. Mismatch ⇒ fail.
  Blocked re-execution ⇒ `error`.
- **G-RULE** (`rule`, critical): re-evaluate the pinned pack version's compiled
  JSONLogic against the recorded `inputs_snapshot`; `fired` mismatch ⇒ fail.
- **G-SUM** (`calc`, critical): Σ line invariants on breakdown-shaped outputs
  (C-03 class): Σ`lines.amount` byte-equal to the linked C-02 output.
- **G-TPL** (`artifact`, critical): stored artifact exists; no StrictUndefined
  leak (no `{{`/`}}` residue); all `required_fields` currently present at (or
  above) the template's `min_verification`; `signable` consistency (a
  `PENDING CAPTURE` placeholder present ⇒ recorded event's `signable` false).
- Grading writes `grader_runs` with `subject_type ∈ field|rule|calc|artifact`,
  `subject_ref` JSON, `claim_id` populated (production) — `test_case_id` stays
  for packet 9. `grader.passed`/`grader.failed` payloads: ids/refs/severity only.
- The `"eval"` consumer maps: `calc.executed` (status executed) → G-CALC (+G-SUM
  when breakdown-shaped), `rule.evaluated` (status evaluated) → G-RULE,
  `template.rendered` → G-TPL. Blocked runs are not graded (nothing executed).
  Field grading (G-VAL) runs on `field.updated` where `source_type='extraction'`.

### 3.3 Autonomy specifics (§3.4 verbatim + constitution)

- **Constitution (hard-coded in `eval_harness`, not data):**
  `triage.ex_gratia` max L1 · `triage.decline_draft` max L1 ·
  `assessment.consistency_flag` max L2 · `assessment.mode_confirm` max L2
  (guide §6) · `pack.note_draft` max L3 · money-adjacent max L3 (**no L4**),
  where money-adjacent = `settlement.*`, `repair.authorize`, `repair.release`,
  `icon.reserve_adjust`, `icon.assessor_payment_request`, `icon.payment_voucher`,
  `salvage.*` (register #71) · `salvage.award` and any `approval.*` id are
  **rejected at load** — not capabilities at all.
- `policies.yaml` rows: `{id, max_level, policy: {...overrides}}`; loader
  rejects `max_level` above the constitution ceiling, unknown levels, and
  forbidden ids. Seed list (register #71): the doc-named Phase-1/2 capability
  ids (intake.mailbox_triage, intake.claim_creation, intake.acknowledge,
  triage.coverage_check, triage.decline_draft, triage.ex_gratia,
  assessment.mode_confirm, assessment.consistency_flag, pack.merge,
  pack.note_draft, pack.route, chase.checklist, repair.authorize,
  repair.invoice_match, repair.completion_detect, salvage.register,
  salvage.publish, salvage.close_and_rank, settlement.eft_match,
  icon.claim_register, icon.reserve_adjust, icon.assessor_payment_request,
  icon.payment_voucher, edms.attach_and_tag, edms.claims_workflow). All start
  `current_level='L1'` except `icon.*`/`settlement.*` start `L0` (shadow —
  projection/finance writes are the risk tier).
- **Counters** from `review.resolved` events with payload
  `{capability_id, resolution: "approved"|"edited"|"rejected", diff:
  {typed_changes: [{path, kind: money|date|party|enum|text}],
  prose_change_ratio: float}}` — interim v0 schema, register #72; PRD-04
  formalises. Material edit = any typed change with kind ∈
  {money,date,party,enum} **or** `prose_change_ratio > 0.15`. Unknown
  capability_id in an event ⇒ ignored with a recorded `error` grader-run? No —
  ⇒ `ops.alert`-free no-op plus log; never a crash, never a guess.
- Grader pass-rate criteria compute over `grader_runs` attributable to the
  capability inside the counting window; an **empty grader window is vacuously
  true** — graders gate outputs at production time, counters gate promotions
  on human resolutions (pinned decision; revisit when agent runs land).
- **Promotion:** `request_promotion` validates, in order: capability known →
  not frozen (`platform_state`) → GP-1 (`settlement.*` ⇒ `GATE_GP1_CLOSED`;
  gate opens only via `platform_state['gp1_open']=true`, absent ⇒ closed) →
  ceiling (constitution ∧ `max_level`) → criteria (§3.4 counters for the
  specific hop; L1→L2 also requires ≥96% grader pass on the window) → sign-offs
  (L1→L2, L2→L3: ≥1 `claims_manager`; L3→L4: `claims_manager` **and** `md` —
  two distinct actors). Denial ⇒ `PromotionDenied(code)` / HTTP 403 with the
  code; success ⇒ level bump, `autonomy_changes` row (evidence = counter
  snapshot), `autonomy.promoted`, ledger row, and for L3: `policy.sampling_rate`
  set to 20 (floor 5).
- **Demotion consumer:** `grader.failed` with `severity='critical'` and payload
  `capability_id` at level L3/L4 ⇒ drop exactly one level, `autonomy.demoted`,
  `ops.alert` event, evidence snapshot — all inside one `dispatch_once` cycle.
  Rolling-20 resolution pass rate <95% (any level ≥L2) ⇒ drop one level.
  **No automatic demotions from the audit-degraded flag** (PACKET-03 posture).
- Sign-off actors recorded verbatim (`user:<ULID>` + claimed role); role
  *verification* is PRD-04 RBAC (register #69).

### 3.4 Reporting (§3.6)

- `GET /eval/capabilities` → per capability: `{id, current_level, max_level,
  pass_rate_window, consecutive_approvals, runs_to_promotion, sampling_rate}`.
- `GET /eval/runs?capability=&grader=` → grader runs, newest first, limit 200.
- `GET /eval/corpus/stats` → `{total, by_origin, by_tag}` from `test_cases`
  (empty until packet 9/open item 7 — endpoint ships now).
- `GET /eval/series` → the four headline series
  (`autonomy_rate`, `no_touch_rate`, `accuracy_by_capability`,
  `median_review_time_seconds`), computed from events + `grader_runs`; empty
  windows return zeros, never errors.

## 4. CTO decisions (D-x) and register entries

- **Register #66** — PRD-03 package = `platform/eval_harness/`, import
  `eval_harness`; models on `claim_core.Base`; ED-1 boundary as before.
- **Register #67** — G-CITE/G-NOTE need model calls (slice 2); G-COMM has no
  producer until PRD-06; G-PROC needs `cop_steps.yaml` (PRD-13) + agent runs
  (Phase 2) → all four registered `pending` with `blocked_on`, visible in the
  registry and `/eval/capabilities` tooling.
- **Register #68** — AR-2 `execute_or_stage` gate module is not PRD-03 scope;
  it ships with the Phase-2 kickoff packet and consumes this controller. The
  §3.3 "blocks at L3+" arm therefore has no caller yet — the gating seam
  (`grade_output`) is the contract it will call.
- **Register #69** — sign-off role verification impossible before PRD-04 RBAC →
  sign-off records store actor + claimed role verbatim; L3→L4 requires two
  distinct actors; console-backed verification lands with PRD-04.
- **Register #70** — authorized `claim_core` change: ledger action map gains
  `autonomy.promoted`/`autonomy.demoted` (PACKET-03 D-map growth clause).
- **Register #71** — capability seed list + money-adjacent set assembled from
  doc-named ids (no single PRD enumerates them); `settlement.eft_match` id is
  provisional pending PRD-12 packet; `icon.*`/`settlement.*` start L0.
  Capture confirmation wanted.
- **Register #72** — `review.resolved` payload v0 schema (capability_id,
  resolution, diff{typed_changes, prose_change_ratio}) defined here so counters
  can run before PRD-04; PRD-04's four-part contract supersedes it.

## 5. Builder guardrails

- **No model calls anywhere** — G-CITE/G-NOTE are pending slots; wiring a live
  LLM into this packet is a defect.
- **No feedback loops.** Grader re-execution (G-CALC re-running a calc, G-RULE
  re-evaluating logic) is a *check*: it must not write `calc_runs`/`rule_runs`,
  must not emit `calc.executed`/`rule.evaluated`, and must not trigger further
  grading. Graders write only `grader_runs` + `grader.passed`/`grader.failed`;
  the `"eval"` consumer never subscribes to grader events.
- **No new review-item types** — grader failures are `EXCEPTION` subtypes;
  sampling uses the existing `SAMPLE_REVIEW` type.
- **Promotion without sign-off must be impossible** at both the Python surface
  and the HTTP surface — no code path skips the checks, including "manual"
  reasons (manual changes go through the same API with sign-offs).
- **Never widen a ceiling.** Pack data may lower `max_level`, never raise;
  forbidden ids (`salvage.award`, `approval.*`) fail the pack load.
- Demotion is the only automatic level change; promotions are always human-signed.
- Event payloads: ids/levels/evidence references — no PII, no field values.
- `claim_core` change limited to the one ledger-map entry (§3); `.github/`,
  `docs/`, `tests/acceptance/`, `tools/ci/`, `pyproject.toml` untouched.
- All packet 01–07 acceptance suites keep passing unmodified.

## 6. Definition of done (ED-7/ED-7a)

- All acceptance tests in `tests/acceptance/test_packet_08_eval_autonomy.py`
  pass unmodified; full suite green on SQLite and PostgreSQL legs.
- ≥80% unit coverage on `platform/eval_harness/`; counter semantics get
  boundary tests (24 vs 25; reset on edit; rolling-window edges; sampling
  formula vectors; two-actor L3→L4).
- Alembic migration chained to head; §3.2 DDL verbatim.
- Gates green: ruff, money-float lint, banned-calls, pytest.
- Runbook content (demotion triage, frozen-promotions procedure, grader-fail
  triage) flagged in the PR description — CTO ships docs.
- ED-11: anything underdetermined → narrowest safe behaviour + proposed
  register entry in the PR description.
