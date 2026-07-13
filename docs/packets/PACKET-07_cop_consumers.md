# PACKET-07 — COP consumers: template engine, routing, outcome execution, FSM guard wiring (PRD-02 slice 2 of 2)

> **Status:** issued · **Builder:** Codex per `AGENTS.md` · **Reviewer:** CTO per `CLAUDE.md`
> **Source spec:** `docs/PRD-02_COP_Runtime_v1.1.md` §2.4–§2.6; PRD-00 §0.4 (guard
> wiring, register #24); PRD-04 §4.3 (closed review-item enum — consumed, not built);
> PRD-08 §8.4 (T-01 payable-slot semantics — consumed, not built); Section 0 ED-1,
> ED-8, ED-11. Precedence: Section 0 → PRD-00 → PRD-02 → PRD-04/PRD-08 → this packet.
> **Depends on:** PACKET-06 merged (`cop_runtime` engines + CTO close-out e13f5b1).
> **Acceptance tests:** `tests/acceptance/test_packet_07_cop_consumers.py` —
> protected, failing by design until this packet is built.
> **Completion:** this packet completes PRD-02 except corpus goldens (register #55)
> and the PRD-13 build pipeline/signing/`pack_registry` (built with PRD-13).

## 1. Scope

**PACKET-06 ratchets (from CTO review — all four land first):**

1. **Single hydration per evaluation (register #59).** `evaluate`/`execute_calc`
   hydrate once; `routing_amount` accepts pre-hydrated fields instead of
   re-hydrating. One R-12 evaluation performs exactly one claim read.
2. **Selective hydration (register #59, authorized `claim_core` change).**
   `hydrate_claim(claim_id, actor, *, paths: Sequence[str] | None = None)` —
   when `paths` is given, return only those current fields and decrypt (and
   ledger-log) only PII fields **among them**. The runtime binds with exactly the
   rule/calc input paths. Evaluating a rule that binds no PII on a PII-bearing
   claim must write **zero** `pii.decrypt` ledger rows.
3. **Per-dialect JSON.** `cop_runtime/models.py` columns use a local
   `JSON().with_variant(JSONB(), "postgresql")` matching migration `0004`.
4. **Honest metadata.** `correlation_id=None` on `rule.evaluated`/`calc.executed`
   (no fake correlation ULIDs); `rule_runs.evaluated_at`/`calc_runs.ts` use the
   app-injected clock (authorized `claim_core` change: `create_app` exposes
   `app.state.clock`).

**PRD-02 slice 2:**

1. **Template engine (§2.4):** Jinja2 `StrictUndefined`; pack registry
   `templates/registry.yaml` `{id, version, channel: email|pdf|field_set, body_ref,
   required_fields[], min_verification: extracted|human_verified, locale}` (`locale`
   default `en-KE`, English-only v1); refuse-to-render when any required field is
   missing **or** below its `min_verification`; calc-slot mechanic (PRD-08 §8.4:
   blocked calc ⇒ literal `PENDING CAPTURE` in the slot + `signable: false`);
   rendered artifacts → `BlobStore` + `template.rendered` event. All motor T-entries
   registered with `status: pending_capture` (bodies/field-lists uncaptured —
   register #61); engine mechanics proven against fixture packs.
2. **Routing (§2.5):** `routing/authority_matrix.yaml` **verbatim** —
   `[{max: 100_000_00, role: asst_claims_manager}, {max: 700_000_00, role:
   claims_manager}, {max: 1_500_000_00, role: gm}, {max: 4_000_000_00, role: md},
   {max: null, role: chairman, side_effects: [render T-03]}]`; `route(amount) →
   RouteResult{role, side_effects}`; **inclusive upper bound** (`amount ≤ band.max`;
   exactly `100_000_00` → `asst_claims_manager`); load-time conformance: bands cover
   `[0, ∞)`, ascending, no gaps, no overlaps, exactly one `max: null` tail —
   violation ⇒ `PackLoadError`. Claim-level amount = `routing_amount` (C-08 payable
   when live, fallback `reserve.total` — already shipped).
3. **Outcome execution:** `execute_outcome(rule_result, actor)` for **fired**
   results only. Verb semantics (§4.3 table); no new event types (PRD-00 catalog is
   closed for this packet: `template.rendered` + existing `field.updated` /
   `review.created` / `claim.status_changed`); review-item events use the PRD-04
   closed 17-type enum only.
4. **FSM guard wiring (register #24 discharge, authorized `claim_core` change):**
   guard-hook registry on `ClaimStateMachine`; `cop_runtime` registers hooks at
   `build_cop_runtime` for the rule-linked edges (§4.4); hook failure ⇒
   `409 TRANSITION_GUARD_BLOCKED` with `blocked_on`; non-rule guards stay recorded
   as `guards_pending` verbatim.
5. **Boundary acceptance (§2.6):** routing band boundaries; template refusal
   negative cases; guard 409s on blocked/not-fired rules.

**Out (unchanged):** PRD-13 pipeline/signing/`pack_registry`/second pack; PDF
binaries + byte-stable regeneration (PRD-08 Chromium policy — register #62);
sending anything anywhere (PRD-06 owns sends; AR-2/AR-3 apply there); review-item
persistence, workspaces, resolution actions (PRD-04); C-04/C-07/C-08 formulas and
all T-verbatim bodies (open items 4/5/6); reopen; autonomy machinery (PRD-03).

## 2. Binding spec quotes (implement verbatim)

PRD-02 §2.4:

> "Jinja2, `StrictUndefined` (any missing variable = render failure, never a
> blank). Registry `{id, version, channel: email|pdf|field_set, body_ref,
> required_fields[], min_verification: extracted|human_verified, locale}` …
> a template **refuses to render** if any required field is below its
> `min_verification` (this is how 'no letter goes out with an unchecked number'
> is enforced structurally). … Rendered artifacts stored to S3 + `template.rendered`."

PRD-02 §2.5:

> "Resolver: `route(amount) → role` used by PRD-08; matrix changes are
> pack-version changes."

> "**Band semantics (v1.1, binding):** inclusive upper bound — `amount ≤
> band.max`. Exactly `100_000_00` → `asst_claims_manager` … the matrix must also
> cover `[0, ∞)` with no gaps or overlaps."

PRD-08 §8.4 (payable slot, binding on the calc-slot mechanic):

> "amount payable = **calc C-08** (registered `blocked_on_inputs` …; until live,
> T-01 renders `PENDING CAPTURE` in this slot and **refuses sign-off**)"

PRD-00 §0.4 via register #24 (guard wiring):

> "Structural guards enforced now; rule-linked edges open to explicit API calls
> with `guards_pending[]` recorded verbatim in the transition event; rule wiring
> replaces them when the COP runtime lands (PACKET-02 D-6)"

PRD-04 §4.3 (consumed):

> "**Review-item type enum — FINAL AND CLOSED (v1.1). Do not add types; new cases
> are `EXCEPTION` subtypes:** `FIELD_VERIFY, DOC_CLASSIFY, DOC_SPLIT,
> CONSISTENCY_FLAG, DRAFT_RELEASE, MODE_CONFIRM, NOTE_REVIEW, PACK_REVIEW,
> EX_GRATIA, EXCEPTION, PROMOTION_SIGNOFF, SAMPLE_REVIEW, PASTE_READBACK_CHECK,
> PROCEED_PARTIAL, KYC_VERIFY, EFT_MATCH, REOPEN_PROMPT`"

## 3. Deliverable

Extend `platform/cop_runtime/` (no new package):

```
platform/cop_runtime/
  templates.py     # TemplateRegistry, render(), TemplateRenderBlocked, calc slots
  routing.py       # AuthorityMatrix, RouteResult, conformance validation
  outcomes.py      # execute_outcome() verb dispatch
  guards.py        # guard-hook implementations registered on the FSM
packs/motor/
  templates/registry.yaml     # all motor T-entries, status: pending_capture
  routing/authority_matrix.yaml
```

Authorized `claim_core` changes — **exactly these, nothing else** (register #60):
1. `ClaimService.hydrate_claim(..., *, paths: Sequence[str] | None = None)`.
2. Guard-hook registry: `ClaimStateMachine.register_guard_hook(edge:
   tuple[ClaimState, ClaimState], hook: Callable[[str], GuardCheck])` where
   `GuardCheck = {passed: bool, blocked_on: list[str]}`; consulted in
   `_apply_transition` before `guards_pending` recording; failed hook ⇒ existing
   `409 TRANSITION_GUARD_BLOCKED` error shape with `blocked_on`. A hook-covered
   edge no longer records its wired guard in `guards_pending` (residual unwired
   guard text stays, see §4.4).
3. `app.state.clock = effective_clock` in `create_app`.
Anything beyond these three: stop and flag in the PR.

### 3.1 Pinned surface (acceptance tests rely on exactly this)

```python
runtime.template_registry(pack_id, version)   # .ids(), .get(id).status/.channel/...
runtime.render(template_id, claim_id, actor) -> RenderResult
#   RenderResult{template_id, template_version, pack_id, pack_version,
#                channel, blob_key, signable: bool, placeholders_pending: list[str]}
#   raises TemplateRenderBlocked(missing_fields=[...], under_verified=[...],
#                                reason: "missing_fields"|"under_verified"|"pending_capture")
runtime.authority_matrix(pack_id, version).route(amount: Money) -> RouteResult
#   RouteResult{role: str, side_effects: list[str]}
runtime.route_for_claim(claim_id, actor) -> RouteResult   # uses routing_amount
runtime.execute_outcome(rule_result: RuleResult, actor) -> OutcomeResult
#   OutcomeResult{action: str, detail: dict}
#   non-fired result -> ValueError; route_approval -> NotImplementedError
claim_core.ClaimService.hydrate_claim(claim_id, actor, *, paths=None)
```

`template.rendered` payload: `{template_id, template_version, pack,
channel, blob_key, signable}` — no field values, no PII.

### 3.2 Template engine

- Registry YAML rows: `{id, version, channel, body_ref, required_fields,
  min_verification, locale, status: live|pending_capture, calc_slots: {var:
  calc_id} (optional), blocked_on (required iff pending_capture)}`. Meta-schema
  validated at pack load; `body_ref` must exist under `templates/` when `live`;
  dangling `calc_slots` calc ids ⇒ `PackLoadError`.
- Render pipeline: registry status `pending_capture` ⇒ `TemplateRenderBlocked
  (reason="pending_capture")` — visible, never a stub letter. Live: selectively
  hydrate exactly `required_fields`; missing ⇒ blocked (`missing_fields`); present
  but `VERIFICATION_RANK[state] < rank[min_verification]` ⇒ blocked
  (`under_verified`). Calc slots: `execute_calc` each; `blocked_on_inputs` ⇒ bind
  the literal string `PENDING CAPTURE`, add var to `placeholders_pending`,
  `signable=False`; executed ⇒ bind output, and `signable=True` only when
  `placeholders_pending` is empty.
- Context binding (pinned): template variables are the `required_fields` paths
  with dots replaced by underscores (`assessment.estimate_total` →
  `{{ assessment_estimate_total }}`), plus one variable per `calc_slots` key.
  No other variables exist; anything else is `StrictUndefined` failure.
- Jinja2 `StrictUndefined`, autoescape off (plain text v1), no custom filters
  beyond `money_kes_display` (integer cents → `KES 1,234.56` — display-only,
  never parsed back). Any undefined variable ⇒ render failure surfaced as
  `TemplateRenderBlocked`, never a blank.
- Channels: `email`/`pdf` render text bodies; `field_set` renders a JSON artifact
  `{path: value}` of the required fields (no Jinja body). PDF **binary** is
  PRD-08's (register #62) — the `pdf` channel stores the rendered text artifact.
- Artifacts: `BlobStore` key `templates/{claim_id}/{template_id}/{ulid}`;
  `template.rendered` event in the same transaction as any run-recording it does.
- Motor registry entries (all `pending_capture`, register #61): T-01 (structure
  PRD-08 §8.4), T-02, T-02b, T-03, T-04, T-05, T-06, T-06r-broker, T-06r-client,
  T-07, T-08, T-08b, T-09, T-10, T-11, T-12, T-13. `blocked_on` names the open
  item (6 for verbatims; PRD-08 §8.4 + field registration for T-01). Required
  field lists (T-02's 7, T-03's 16) are **not invented** — empty until capture.

### 3.3 Routing

- Matrix YAML verbatim from §2.5 (the five bands above). Loader conformance:
  ascending strictly-increasing `max` values, first band covers from 0, exactly
  one terminal `max: null`, roles non-empty — else `PackLoadError`.
- `route(amount)`: smallest band with `amount <= max` (null = ∞). Negative
  amount ⇒ `ValueError` (never guess).
- `route_for_claim`: `routing_amount` (payable→fallback); `None` ⇒ `LookupError`
  (`ROUTING_AMOUNT_UNAVAILABLE`) — no default band.

### 3.4 Outcome execution (verbs — §2.2 enum, closed)

`execute_outcome(rule_result, actor)`; `rule_result.fired is not True` ⇒
`ValueError`. All writes/events carry provenance back to the producing rule run.

| Verb | Execution |
|---|---|
| `set_field` | One write via `claim_core` public write path: `source_type="rule"`, `verification_state="system_confirmed"`, `source_ref={rule_id, rule_version, rule_run_id}` (register #65); value = `outcome.value`, or `outcome.value_map[inputs_snapshot[outcome.value_from]]` (unmapped key ⇒ `ValueError`, never guess). `HUMAN_OVERRIDE_PROTECTED` 409 propagates — `claim_core` already records the protected attempt; no swallow. |
| `route_review` | `review.created` event `{type: <pack review_routes map>, route, role, rule_id, rule_run_id}`. Mapping is pack config `review_routes: {EX_GRATIA_REVIEW: EX_GRATIA}` (register #63); unmapped route ⇒ `LookupError`. Types restricted to the PRD-04 closed enum. |
| `propose_decline` | `review.created` `{type: DRAFT_RELEASE, subtype: decline_draft, draft_template, rule_id, rule_run_id}`. **No transition, no decline call, no release** — release is human, permanently (guide §3.11). |
| `block` | No side effect: returns `OutcomeResult(action="block")`. Blocks act through guard hooks (§4.4), not through execution. |
| `emit_event` | Attempts the outcome's `draft_template` render; `TemplateRenderBlocked` ⇒ `review.created` `{type: EXCEPTION, subtype: template_pending_capture, template_id, rule_id, rule_run_id}` — the alert slot is visible, its content never invented (register #63). No synthetic event type (catalog closed). |
| `route_approval` | `NotImplementedError` — no motor rule produces it; PRD-08 wires approval flow (register #63). |
| anything else | Rejected at pack load: loader now validates `outcome.action` against the six-verb enum (`PackLoadError` on a seventh). |

### 3.5 FSM guard wiring (register #24)

`cop_runtime.guards` registers hooks at `build_cop_runtime`:

| Edge | Hook |
|---|---|
| `REPORT_RECEIVED → WRITE_OFF` | `evaluate("R-05")`: passes iff `status=="evaluated" and fired is True`; blocked or not-fired ⇒ fail, `blocked_on=["R-05 …"]` with the rule status. |
| `IN_REPAIR → REINSPECTION` | `evaluate("R-08")`: same semantics. |
| `SURRENDER_CHECKLIST → SETTLEMENT` | `evaluate("R-13")` must be `evaluated and not fired` **and** `evaluate("R-14")` must be `evaluated and not fired`. R-14 is `blocked_on_inputs` (register #49) ⇒ this edge is conservatively unreachable until capture — deliberate and visible (register #64; aligns with the C-07 stop at SALVAGE_BIDDING, guide §6). The unwired "checklist complete" guard text stays in `guards_pending`. |

Every hook failure ⇒ `409 TRANSITION_GUARD_BLOCKED` `{blocked_on: [...]}`; the
claim does not move; the failed `evaluate` calls are themselves recorded in
`rule_runs` (auditable). Unwired guards (coverage/estimate/report/manifest/
officer-sign/C-02) keep the PACKET-02 `guards_pending` behaviour unchanged.

## 4. CTO decisions (D-x) and register entries

- **Register #60** — authorized `claim_core` changes for slice 2: selective
  hydration `paths=`, FSM guard-hook registry, `app.state.clock`. Nothing else.
- **Register #61** — all motor template bodies + required-field lists (T-02's 7
  merge fields, T-03's 16 fields, T-01 body prose) uncaptured → every motor
  registry entry ships `pending_capture` with `blocked_on`; engine mechanics
  proven via fixture packs; T-01 body lands with the PRD-08 packet.
- **Register #62** — `pdf` channel stores the rendered text artifact; PDF binary +
  byte-stable regeneration land with PRD-08's network-disabled Chromium policy.
- **Register #63** — verb-mapping decisions: route→review-type map is pack config
  (`review_routes`); `propose_decline` ⇒ `DRAFT_RELEASE`; `emit_event` executes
  the draft-template attempt only (event catalog closed — no new event type);
  `route_approval` execution deferred to PRD-08 (no producing motor rule).
- **Register #64** — SETTLEMENT edge conservatively hard-blocked while R-14
  inputs are uncaptured; consistent with write-off claims stopping at
  SALVAGE_BIDDING until C-07 lands.
- **Register #65** — rule-written fields: `source_type="rule"`,
  `verification_state="system_confirmed"`, `source_ref={rule_id, rule_version,
  rule_run_id}` — reconstructable to the exact run.

## 5. Builder guardrails

- **Nothing sends.** No email, no external write, no adapter op. Templates render
  to the blob store, full stop. AR-2 grep must stay clean.
- **No review-item type outside the PRD-04 seventeen** (guide §3.9). New cases
  are `EXCEPTION` subtypes.
- **No autonomy shortcuts** (guide §3.11): decline drafts are never released,
  notes are never signed, nothing here auto-approves anything.
- **No template content invention.** A `pending_capture` template never renders
  prose. Fixture templates live in test fixtures, not in `packs/motor/`.
- `claim_core` changes limited to the three §3 items; each needs its own focused
  diff. Reviewer will diff `claim_core` first.
- Money display formatting is one-way (`money_kes_display`); nothing parses
  rendered strings back into `Money`.
- All packet 01–06 acceptance tests keep passing unmodified; `.github/`,
  `docs/`, `tests/acceptance/`, `tools/ci/`, `pyproject.toml` untouched.
- Event payloads carry ids/keys/status, never field values or PII.

## 6. Definition of done (ED-7/ED-7a)

- All acceptance tests in `tests/acceptance/test_packet_07_cop_consumers.py`
  pass unmodified; full suite green on SQLite and PostgreSQL legs.
- ≥80% unit coverage on `platform/cop_runtime/`; template refusal and routing
  conformance get exhaustive negative tests; `packs/motor/calcs/calcs.py` stays
  100%.
- Gates green: ruff, money-float lint, banned-calls, pytest.
- No new tables (registry/matrix are pack data; artifacts live in the blob
  store); if you believe DDL is needed, stop and flag.
- Runbook content (render-refusal triage, guard-blocked triage) flagged in the
  PR description — CTO ships docs.
- ED-11: anything underdetermined → narrowest safe behaviour + proposed register
  entry in the PR description.
