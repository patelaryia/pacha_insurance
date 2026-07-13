# PACKET-06 — COP engines: pack loader, rule runtime, calc registry, motor rules/calcs (PRD-02 slice 1 of 2)

> **Status:** issued · **Builder:** Codex per `AGENTS.md` · **Reviewer:** CTO per `CLAUDE.md`
> **Source spec:** `docs/PRD-02_COP_Runtime_v1.1.md` §2.1–§2.3 (+§2.6 rule/calc acceptance);
> PRD-13 §13.2/§13.3 (pack layout + pinning, consumed early per guide §2);
> Section 0 ED-1, ED-8, ED-11. Precedence: Section 0 → PRD-02 → PRD-13 → this packet.
> **Depends on:** PACKET-04 merged on main (`claim_core` + `doc_intel` substrate).
> Independent of PACKET-05 (in flight, disjoint package) — see migration guardrail §6.
> **Acceptance tests:** `tests/acceptance/test_packet_06_cop_engines.py` —
> protected, failing by design until this packet is built.
> **Packet 7 (next, PRD-02 slice 2):** template engine (§2.4), routing matrix + resolver
> (§2.5), outcome-verb *execution* (review items, transitions, set_field commits,
> T-03 render), FSM `guards_pending` rewiring (register #24), PRD-13 build
> pipeline/signing/`pack_registry`.

## 1. Scope

**In:** everything in PRD-02 §2.2–§2.3 up to and including *evaluation and recording* —

1. New package **`platform/cop_runtime/`**, import `cop_runtime` (register #46).
2. **Pack loader (interim):** reads a pack directory (`packs/motor/`), validates every
   YAML against a runtime meta-schema, compiles rule `when` clauses to **JSONLogic**
   at load (`json-logic-py` — new dependency, named by the PRD), **fails on
   unresolvable field paths**, AST-sandbox-checks `calcs/calcs.py`. Full PRD-13 build
   pipeline (tar+sha signing, `pack_registry`, corpus golden run) is packet 7+/PRD-13.
3. **Rule runtime:** `evaluate(rule_id, claim_id) → RuleResult{fired, outcome,
   inputs_snapshot, rule_version}`; inputs bound from claim field paths via
   `claim_core` hydration; **missing/unverified required input ⇒ status
   `blocked_on_inputs`, never a silent false**; every evaluation writes `rule_runs`
   and emits `rule.evaluated` in one transaction.
4. **All 16 motor rule slots as pack data** (`packs/motor/rules/R-*.yaml`) — the §2.2
   register implemented exactly; blocked slots visibly `blocked_on_inputs` (§4.3 table).
5. **Calc registry:** `@calc("C-01", version="1.0.0")` decorator, `Money` = integer
   KES cents (never float), C-01..C-06 implemented, **C-07 + C-08 registered
   `blocked_on_inputs`**; every execution writes `calc_runs` + emits `calc.executed`.
   Calcs live in the pack (`packs/motor/calcs/calcs.py`), sandboxed per PRD-13 §13.2.
6. **Tables** `rule_runs` (register #47) + `calc_runs` (§2.3 columns binding).
7. **Reconstructability (§2.6):** `rule_runs`/`calc_runs` carry inputs + versions
   sufficient to reconstruct any figure on any claim.
8. **Pack-version mechanics:** claims pin `pack@version` at creation (PRD-00 schema,
   already live); the runtime evaluates with the claim's pinned pack version; a rule
   change deploys as a pack version bump with **zero code release** (§2.6);
   `effective_from` in rule YAML is documentation metadata, never evaluated (§2.2).

**Out (packet 7):** template engine + registry + refuse-to-render (§2.4); routing
authority matrix + `route(amount)` resolver (§2.5); *execution* of outcome verbs
(`propose_decline`/`route_review`/`set_field`/`block`/`route_approval`/`emit_event`
side-effects — this packet returns and records outcome **data** only); FSM guard
rewiring (`guards_pending`, register #24); T-03 render for R-12.
**Out (elsewhere):** corpus golden runs (open item 7 + PRD-03 harness — register #55
mirrors #36); PRD-13 signing/`pack_registry`/second-pack proof; pack repo
reorganisation to the full §13.2 layout (`fields.yaml` stays at its PACKET-04
location — packet 7 or PRD-13 moves it).

## 2. Binding spec quotes (implement verbatim)

PRD-02 §2.2, rule format + contract:

> "Rules are YAML in the pack repo, compiled to **JSONLogic** at pack build
> (deterministic, auditable, portable; evaluated by `json-logic-py` against a bound
> input dict of claim field paths — no arbitrary code in rules, ever)"

> "Runtime contract: `evaluate(rule_id, claim_id) → RuleResult{fired, outcome,
> inputs_snapshot, rule_version}`; every evaluation writes `rule_runs` + emits
> `rule.evaluated`. **Missing/unverified required input ⇒ status `blocked_on_inputs`,
> never a silent false.**"

> "`effective_from` … DOCUMENTATION METADATA ONLY (v1.1): never evaluated at runtime
> and NOT in the runtime schema. Pack version pinning (PRD-13 §13.3) is the sole
> versioning authority."

PRD-02 §2.2, R-02 (the YAML in the PRD is the spec — ship it verbatim, including the
`exception` escalation block and `context_fields`).

PRD-02 §2.3, calcs:

> "Pure Python functions, decorator-registered `@calc("C-01", version="1.0.0")`,
> typed with `Money` (integer KES cents — **never floats for money anywhere in the
> platform**), 100% unit-test coverage mandatory, every execution →
> `calc_runs(calc_id, version, inputs, output, claim_id, ts)` + `calc.executed`."

> "`C-08` **payable** — registered now, status `blocked_on_inputs`, formula =
> top-priority CM capture (open item 5)"

PRD-13 §13.2, calc sandbox (binding on the loader built here):

> "`calcs/calcs.py` # C-01..C-07 — the ONLY code in a pack; sandboxed: imports
> restricted to stdlib decimal/datetime + platform Money; AST-checked at build
> (no IO, no network, no eval)"

PRD-13 §13.3, pinning:

> "claims pin `pack@version` at creation … Rule changes therefore never silently
> alter an open claim's evaluation history — `rule_runs` keep the version that
> actually ran."

PRD-02 §2.6, acceptance (this packet's subset):

> "All 16 rule slots registered (❓ ones in `blocked_on_inputs`/`pending_capture`,
> visibly); golden tests per rule/calc … incl. boundary values (estimate = excess
> exactly; quote = 50.0% PAV exactly — spec: R-05 fires on strictly-greater); rule
> change deploys as pack version bump with zero code release; `rule_runs`/`calc_runs`
> reconstruct any figure on any claim to inputs + versions."

## 3. Deliverable

```
platform/cop_runtime/
  __init__.py       # curated exports: build_cop_runtime, CopRuntime, Money,
                    #   RuleResult, CalcResult, PackLoadError
  money.py          # Money = NewType("Money", int) — KES cents (ED-8)
  pack_loader.py    # load_pack(path) -> LoadedPack; meta-schema validation,
                    #   JSONLogic compile, path resolution, calc AST sandbox
  rules.py          # RuleRegistry, evaluate(), input binder, rule_runs writer
  calcs.py          # calc registry/decorator, execute_calc(), calc_runs writer
  models.py         # RuleRun, CalcRun on claim_core Base
  runtime.py        # CopRuntime + build_cop_runtime(app, pack_paths=[...])
packs/motor/
  pack.yaml         # id: motor, version: 1.0.0, platform_min_version,
                    #   display strings, config: (late_days, desk_physical_threshold
                    #   — blocked_on_inputs placeholders)
  rules/R-01.yaml … R-16.yaml   # one file per slot, §4.3 table
  calcs/calcs.py    # C-01..C-08 (C-07/C-08 blocked stubs)
  fields.yaml       # EXTEND (in place) with the §4.4 rule/calc input paths
```

Cross-package access **only** via `claim_core` root exports and `app.state`
(`claim_service`, `engine`) — ED-1 boundary, same posture as `doc_intel`
(register #33). No `claim_core.<internal>` imports.

### 3.1 Runtime contract (pinned by acceptance tests)

```python
from cop_runtime import build_cop_runtime
runtime = build_cop_runtime(app, pack_paths=[Path("packs/motor")])
# - app.state.cop_runtime = runtime
# - runtime.rule_registry(pack_id, version) -> RuleRegistry
#     .ids() -> list[str] (16 for motor@1.0.0); .get(rule_id).status
# - runtime.calc_registry(pack_id, version) -> CalcRegistry (.ids(), .get(id).status)
# - runtime.evaluate(rule_id, claim_id, actor) -> RuleResult
# - runtime.execute_calc(calc_id, claim_id, actor) -> CalcResult
# - runtime.routing_amount(claim_id, actor) -> Money | None
#     (C-08 payable when live, else reserve.total field, else None — §2.5 fallback;
#      the routing *resolver* is packet 7, this helper feeds R-12 now)
```

`RuleResult`: `{rule_id, rule_version, pack_id, pack_version, status:
"evaluated"|"blocked_on_inputs", fired: bool|None, outcome: dict|None,
inputs_snapshot: dict, missing_inputs: list[str]}` — `fired` is `None` (never
`False`) when blocked; `missing_inputs` names the unbound paths.

`CalcResult`: `{calc_id, calc_version, pack_id, pack_version, status:
"executed"|"blocked_on_inputs", output, inputs: dict, missing_inputs: list[str]}`.

Errors (never guess): unknown `rule_id`/`calc_id` → `LookupError`; claim pinned to a
pack version not loaded → `LookupError` (message contains `PACK_VERSION_NOT_LOADED`);
pack that fails validation/compilation/sandbox → `PackLoadError` at load, nothing
partially registered.

### 3.2 Pack loader

- `pack.yaml`: `{id, version (semver), platform_min_version, display_strings,
  config}`. Config placeholders carry `{value: null, status: blocked_on_inputs}` —
  a rule whose inputs reference a placeholder config key is `blocked_on_inputs`.
- Rule runtime meta-schema (register #47): `{id, name, applies_to, status:
  live|blocked_on_inputs, blocked_on (required iff blocked), inputs: {alias: path},
  when, outcome, version}`. `effective_from` is tolerated in the YAML and **stripped**
  before compilation — documentation metadata only (§2.2). Unknown keys → `PackLoadError`.
- Input paths resolve against the field dictionary (core + pack extensions — load
  `fields.yaml` extensions first). Unresolvable **input** path → `PackLoadError`.
  `pack.<key>` inputs resolve against `pack.yaml` config. Outcome `context_fields`
  are consumer hints, never bound as inputs: unresolvable ones load with a visible
  `pending_field_registration` marker (register #56) — evaluation is unaffected.
- `when` compiles to JSONLogic; compile failure → `PackLoadError`. No code in rules.
- Calc sandbox (PRD-13 §13.2): AST check of `calcs/calcs.py` — imports restricted to
  stdlib `decimal`/`datetime` + `cop_runtime.money`/`cop_runtime.calcs` (decorator);
  no IO, no network, no `eval`/`exec`/`open`/`__import__`/attribute escapes. Violation
  → `PackLoadError`. 
- Multiple packs/versions loadable side by side; `(pack_id, version)` keys the
  registries. Loading the same `(id, version)` twice → `PackLoadError` (no silent
  replace).

### 3.3 Rule evaluation

- Resolve the claim's pinned `pack@version` (PRD-00 `claims.pack_version`, format
  `motor@1.0.0`) → that pack's registry. Not loaded → `PACK_VERSION_NOT_LOADED`.
- Slot `status: blocked_on_inputs` → `RuleResult` blocked, `missing_inputs` =
  `blocked_on` list. Still writes `rule_runs` + emits `rule.evaluated` (visible,
  auditable — never a silent skip).
- Bind `inputs`: hydrate via `claim_core` public read path; for each alias→path,
  the **current field** must exist and be at a committed verification state
  (`extracted`/`human_verified`/`system_confirmed`; per-input `min_verification`
  override slot — register #56). Any input unbound → blocked, `fired=None`.
  Money values bind as integer cents; date/datetime values bind as epoch days (UTC)
  so JSONLogic arithmetic stays numeric (register #53).
- Evaluate compiled JSONLogic against the bound dict → `fired: bool`; `outcome` =
  the rule's outcome block (data, returned verbatim) when fired, else `None`.
- One transaction: insert `rule_runs` row + `record_event("rule.evaluated",
  payload={rule_id, rule_version, pack, status, fired})` on the same session.
  Payloads carry **no field values** (inputs live in `rule_runs.inputs_snapshot`;
  PII posture unchanged).

### 3.4 `rule_runs` / `calc_runs` DDL

- `rule_runs` (register #47, designed locally): `id` ULID PK, `claim_id` FK,
  `rule_id`, `rule_version`, `pack_id`, `pack_version`, `status`, `fired` (nullable
  BOOL), `outcome` JSON, `inputs_snapshot` JSON, `missing_inputs` JSON, `actor`,
  `evaluated_at`. Append-only — no update path.
- `calc_runs` — §2.3 column list is **binding**: `calc_id, version, inputs, output,
  claim_id, ts`; plus `id` ULID PK, `pack_id`, `pack_version`, `status`,
  `missing_inputs`, `actor` (additions per register #47). Append-only.
- Alembic migration in `claim_core/alembic/versions/`, revision chained to the
  repository head **at merge time** (PACKET-05 is in flight — rebase and renumber if
  it lands first; single linear chain, no branches).

### 3.5 Motor v1 rule register (build exactly — §2.2)

| Slot | Status | `when` (compiled) / notes |
|---|---|---|
| R-01 coverage-validity composite | `blocked_on_inputs` | ❓ open item 4 |
| R-02 below_excess_decline | live | PRD YAML verbatim: `{"<=":[estimate, excess]}`; inputs `{estimate: assessment.estimate_total, excess: policy.excess_amount}`; outcome `propose_decline` + `draft_template: T-07` + exception block (EX_GRATIA_REVIEW / claims_manager / context_fields `[client.loss_ratio, client.premium_history]` → `pending_field_registration`, register #56). Fires at equality. |
| R-03 ex_gratia_escalation | live | fires unconditionally when evaluated (trigger predicate unstated — register #51); outcome pure `route_review{route: EX_GRATIA_REVIEW, role: claims_manager}`. **No auto path — permanently** (guide §3.11: `triage.ex_gratia` = L1). |
| R-04 | `blocked_on_inputs` | ❓ open item 4 |
| R-05 write_off | live | strictly-greater, integer-exact compile: `{">": [{"*":[quote, 2]}, {"min":[pav, sum_insured]}]}` (≡ `quote > 0.5×min(pav,si)`, no float 0.5 — register #53); inputs `{quote: assessment.agreed_quote, pav: assessment.pav, sum_insured: policy.sum_insured}`; outcome `set_field{path: assessment.write_off_indicated, value: true}` (transition verb absent from §2.2 enum — register #52; FSM edge wiring is packet 7). Quote = 50.0% exactly → **not** fired. |
| R-06 desk_vs_physical | `blocked_on_inputs` | ❓Q-02 — config placeholder `pack.desk_physical_threshold` per §2.2, guide §6 |
| R-07 multi_assessor_lowest | `blocked_on_inputs` | multi-assessor quote paths owned by PRD-07 (FX-1) — register #49 |
| R-08 reinspection_physical | live | `{"or":[{">":[quote, 50_000_00]}, {"==":[parts_replaced, true]}]}`; inputs `{quote: assessment.agreed_quote, parts_replaced: assessment.parts_replaced}`; outcome `set_field{path: assessment.reinspection_mode, value: physical}`. 50_000_00 → not fired; 50_000_01 → fired (guide §4 boundary). |
| R-09 validation_assessor_distinct | live | `{"==":[validation_assessor, initial_assessor]}` → fired = violation; inputs `{validation_assessor: assessment.validation_assessor_id, initial_assessor: assessment.initial_assessor_id}`; outcome `block`. |
| R-10 late_intimation | `blocked_on_inputs` | `pack.late_days` value uncaptured — placeholder config + blocked, register #50 (same treatment §2.2 prescribes for R-06) |
| R-11 abstract_waiver | `blocked_on_inputs` | ❓ `pack.abstract_waiver_conditions`, open item 4 |
| R-12 four_million_alert | live | `{">":[amount, 4_000_000_00]}`; input `amount` bound via `runtime.routing_amount()` (C-08 payable when live, fallback `reserve.total` — §2.5, safe upward-only fallback); outcome `emit_event` + `draft_template: T-03` (render is packet 7). Exactly 4M → not fired. |
| R-13 settlement_salvage_block | live | `{"!":{"and":[logbook_held, keys_held]}}`; inputs `{logbook_held: salvage.logbook_held, keys_held: salvage.keys_held}`; outcome `block` (verb per §2.2). Missing either field → `blocked_on_inputs` (which also cannot pass a gate — safe). |
| R-14 bank_interest_discharge | `blocked_on_inputs` | discharge-received signal + payee path owned by PRD-06/PRD-12 — register #49 |
| R-15 retain_surrender_variant | live | `{"in":[election, ["retain","surrender"]]}`; input `{election: salvage.election}`; outcome `set_field{path: settlement.c07_variant, value_from: election, value_map: {retain: retained, surrender: surrendered}}` (register #52; C-07 itself stays blocked). |
| R-16 | `blocked_on_inputs` | ❓ open item 4 |

### 3.6 Calc registry (§2.3)

- Decorator declares the claim-path input binding (register #47):
  `@calc("C-01", version="1.0.0", inputs={"sum_insured": "policy.sum_insured"})`.
  `execute_calc` binds exactly like rules (committed field required; missing →
  `blocked_on_inputs`, `calc_runs` row + `calc.executed` still written).
- **C-01** excess = `clamp(2.5% × sum_insured, 15_000_00, 100_000_00)` — Decimal
  arithmetic, `ROUND_HALF_EVEN` to integer cents (register #53); output `Money`.
- **C-02** reserve = `agreed_quote + assessor_fee + reinspection_fee`
  (inputs: `assessment.agreed_quote`, `assessment.assessor_fee`,
  `assessment.reinspection_fee` — path names register #48).
- **C-03** breakdown = lines `[{category, payee_party_id, amount,
  parent_reserve_id}]`: one line per supplier in `assessment.supplier_lines`
  (object field: `[{payee_party_id, amount}]`), `garage_residual` =
  agreed_quote − Σsupplier (payee `assessment.garage_party_id`), `assessor` =
  assessor_fee (payee `assessment.assessor_party_id`), `reinspection_residual` =
  reinspection_fee. **Invariant Σlines = C-02 output** — assert it; violation is a
  bug, not a review item. `parent_reserve_id` = id of the most recent C-02
  `calc_runs` row for the claim; none → `blocked_on_inputs` (register #54).
- **C-05** savings = `estimate_total − agreed_quote`; when `supplier_lines` present,
  output also carries per-supplier deltas.
- **C-06** write-off reserve = `agreed_value + assessor_fee + towing`
  (`assessment.agreed_value`, `assessment.towing_fee` — register #48).
- **C-07** settlement (both variants) + **C-08** payable: registered, status
  `blocked_on_inputs`, executing → blocked result, never a number (guide §6, open
  item 5). C-04 ❓ — slot `blocked_on_inputs` (open item 4).
- 100% unit-test coverage on `packs/motor/calcs/calcs.py` — mandatory (§2.3).

### 3.7 Pack field extensions (`packs/motor/fields.yaml`, extend in place)

Add (register #48): `policy.excess_amount` (money — PRD-02 verbatim; core
`policy.excess` conflict logged, **do not remap silently**), `policy.sum_insured`
(money), `assessment.parts_replaced` (bool), `assessment.validation_assessor_id` /
`assessment.initial_assessor_id` (string), `assessment.assessor_fee` /
`assessment.reinspection_fee` / `assessment.towing_fee` / `assessment.agreed_value`
(money), `assessment.supplier_lines` (object), `assessment.garage_party_id` /
`assessment.assessor_party_id` (string), `assessment.write_off_indicated` (bool),
`assessment.reinspection_mode` (string), `salvage.logbook_held` / `salvage.keys_held`
(bool), `salvage.election` (enum `retain|surrender`), `settlement.c07_variant`
(string). All `pii_class: none`.

## 4. CTO decisions (D-x) and register entries

Numbering continues after PACKET-05's #39–45 (in flight on its branch).

- **Register #46** — PRD-02 package = `platform/cop_runtime/`, import `cop_runtime`;
  models on `claim_core.Base`; cross-package access via root exports + `app.state`
  only (ED-1, mirrors #33).
- **Register #47** — `rule_runs` DDL designed locally (PRD gives contract, no DDL);
  `calc_runs` §2.3 columns binding + `id`/`pack_id`/`pack_version`/`status`/
  `missing_inputs`/`actor` added; calc input binding declared in the `@calc`
  decorator (PRD names no mechanism).
- **Register #48** — input-path gaps: PRD-02 names `policy.excess_amount` but the
  core dictionary registered `policy.excess` (PACKET-01 subset) — both exist until a
  spec round reconciles; fee/party/salvage/election paths unnamed by any PRD →
  locally named pack extensions (§3.7), capture confirmation needed.
- **Register #49** — R-07 (multi-assessor quote paths, PRD-07 FX-1) and R-14
  (discharge-received signal + payee, PRD-06/12) reference machinery owned by later
  PRDs → slots ship `blocked_on_inputs` naming the missing inputs.
- **Register #50** — `pack.late_days` value uncaptured anywhere in the doc set →
  R-10 ships config placeholder + `blocked_on_inputs` (the §2.2 R-06 treatment).
- **Register #51** — R-03 trigger predicate unstated → fires unconditionally when
  evaluated; outcome pure `route_review`; invocation timing is the caller's
  (triage, PRD-05). No auto path exists.
- **Register #52** — §2.2 outcome-verb enum has no transition/selection verb, yet
  R-05 says "transition WRITE_OFF" and R-15 "selects C-07 variant" → `set_field`
  outcomes (`assessment.write_off_indicated`; `settlement.c07_variant` via
  `value_from`+`value_map`); execution + FSM wiring land in packet 7.
- **Register #53** — money percentage arithmetic: Decimal `ROUND_HALF_EVEN` to
  integer cents (C-01); R-05 compiled integer-exact (`quote×2 > min`) to keep floats
  out of money comparisons; date inputs bind as UTC epoch days. Rounding mode needs
  capture confirmation.
- **Register #54** — C-03 `parent_reserve_id` = most recent C-02 `calc_runs.id` for
  the claim; absent → `blocked_on_inputs`.
- **Register #55** — §2.6 golden tests "from corpus cases" not computable (corpus =
  open item 7, harness = PRD-03) → synthetic boundary goldens now; mirror of #36.
- **Register #56** — "unverified required input" semantics: default = any committed
  verification state qualifies; per-input `min_verification` override slot in rule
  YAML; outcome `context_fields` referencing unregistered `client.*` paths load with
  a `pending_field_registration` marker and are never bound as inputs.

## 5. Builder guardrails

- **No outcome execution.** This packet never creates review items, never
  transitions claims, never renders templates, never writes claim fields from rule
  outcomes. Evaluation + recording only. (Packet 7 owns execution behind the
  existing invariants.)
- **No payment anything, no L4 anything** (guide §3.6/§3.11).
- Money: integer cents end-to-end; no `float` in any Money-typed signature (ED-8
  lint); JSONLogic money comparisons compiled integer-exact per register #53.
- Never guess: every unbound input, unknown id, unloaded pack version, invalid pack
  → blocked status or loud error. **No code path silently picks.**
- Rules are data: no Python in `rules/*.yaml`; no rule/threshold/model-id literals
  in `platform/cop_runtime/` (config-over-code, guide §4) — thresholds live in the
  YAML/pack config only.
- `claim_core` source untouched except: nothing. (If you believe a `claim_core`
  change is required, stop and flag it in the PR — reviewer decides.) The Alembic
  migration file is additive only.
- Event emissions limited to `rule.evaluated` / `calc.executed` (already in the
  PRD-00 §0.3 catalog). Payloads carry ids/versions/status, never field values.
- `.github/`, `docs/` (except nothing), `tests/acceptance/` protected — builder does
  not modify; flag doc/runbook content in the PR description (CTO ships docs).
- All packet 01–05 acceptance tests keep passing unmodified.

## 6. Definition of done (ED-7/ED-7a)

- All acceptance tests in `tests/acceptance/test_packet_06_cop_engines.py` pass
  unmodified; full suite green on SQLite (no env) and PostgreSQL (`DATABASE_URL`).
- **100% unit coverage on `packs/motor/calcs/calcs.py`** (§2.3 mandate); ≥80% on
  `platform/cop_runtime/`; loader gets negative tests (bad YAML, unresolvable path,
  sandbox violations, duplicate load).
- Gates green: ruff, ED-8 money-float lint, AR-2 banned-calls, pytest.
- Alembic migration chained to repo head at merge time (see §3.4; rebase over
  PACKET-05 if it merges first).
- `json-logic-py` added to `requirements.txt` (PRD-named dependency).
- Runbook content (pack load failure modes, blocked-rule triage) flagged in PR
  description — CTO ships the docs page.
- ED-11: anything underdetermined → narrowest safe behaviour + register entry.
