## PRD-02 — Rules, Calculations & Template Engine (COP Runtime) (build spec)

> **v1.1** — incorporates the CTO decision round of 2026-07. Where this document conflicts with Section 0 (Shared Engineering Decisions) or Section 0.5 (Shared Agent Runtime), those files win. Anything underdetermined: follow ED-11 (never decide locally — route to the open-items register).

### 2.1 Purpose

Rules/calcs/templates/routing are **versioned data** executed by a generic runtime. This is the pack mechanism.

### 2.2 COP rule format

Rules are YAML in the pack repo, compiled to **JSONLogic** at pack build (deterministic, auditable, portable; evaluated by `json-logic-py` against a bound input dict of claim field paths — no arbitrary code in rules, ever):

yaml

```yaml
id: R-02
name: below_excess_decline
applies_to: motor
inputs: {estimate: assessment.estimate_total, excess: policy.excess_amount}
when: {"<=": [{var: estimate}, {var: excess}]}
outcome:
  action: propose_decline          # runtime verbs: propose_decline | route_review |
  draft_template: T-07             #   set_field | block | route_approval | emit_event
  exception:                       # DET-P: the escalation path is part of the rule
    route: EX_GRATIA_REVIEW
    role: claims_manager
    context_fields: [client.loss_ratio, client.premium_history]
version: 1.0.0
effective_from: 2026-08-01   # DOCUMENTATION METADATA ONLY (v1.1): never evaluated at
                             # runtime and NOT in the runtime schema. Pack version
                             # pinning (PRD-13 §13.3) is the sole versioning authority.
```

Runtime contract: `evaluate(rule_id, claim_id) → RuleResult{fired, outcome, inputs_snapshot, rule_version}`; every evaluation writes `rule_runs` + emits `rule.evaluated`. **Missing/unverified required input ⇒ status `blocked_on_inputs`, never a silent false.**

**Motor v1 rule register** (build exactly; ❓ = value/definition to capture from discovery log — owner Aryia, blocking for those rules only): R-01 ❓ coverage-validity composite · R-02 below-excess (above) · R-03 ex-gratia escalation (pure `route_review`, no auto path — permanently) · R-04 ❓ · R-05 write-off: `agreed_quote > 0.5 × min(pav, sum_insured)` → transition WRITE_OFF · R-06 desk-vs-physical threshold ❓Q-02 (ship with config placeholder + `blocked_on_inputs` until value confirmed) · R-07 multi-assessor → lowest agreed quote selected, comparison table artifact · R-08 re-inspection: physical if `agreed_quote > 50_000 OR parts_replaced` · R-09 validation assessor ≠ initial assessor (block on violation) · R-10 late intimation: `intimation.date − loss.date > pack.late_days` → flag review · R-11 police abstract waivable when `pack.abstract_waiver_conditions` ❓confirm conditions · R-12 `amount > 4_000_000_00` (KES 4M, cents per ED-8; amount = routing amount per §2.5) → render T-03 16-field alert to Head of Claims + MD · R-13 settlement hard block unless `salvage.logbook_held ∧ salvage.keys_held` (verb: `block`) · R-14 `logbook.bank_interest.present` → require `bank_discharge_letter` received ∧ payee=bank (block + chase item) · R-15 retain/surrender → selects C-07 variant · R-16 ❓.

### 2.3 Calculation registry

Pure Python functions, decorator-registered `@calc("C-01", version="1.0.0")`, typed with `Money` (integer KES cents — **never floats for money anywhere in the platform**), 100% unit-test coverage mandatory, every execution → `calc_runs(calc_id, version, inputs, output, claim_id, ts)` + `calc.executed`.

`C-01` excess = `clamp(0.025 × sum_insured, 15_000_00, 100_000_00)` · `C-02` reserve = agreed_quote + assessor_fee + reinspection_fee · `C-03` breakdown = linked lines {supplier_i…, garage_residual, assessor, reinspection_residual}, invariant Σlines = C-02, each line {category, payee_party_id, amount, parent_reserve_id} · `C-05` savings = estimate_total − agreed_quote (+ per-supplier-line deltas) · `C-06` write-off reserve = agreed_value + assessor_fee + towing · `C-07` settlement: surrendered = min(pav, sum_insured) − excess; retained = min(pav, sum_insured) − excess − salvage_value — **both variants stubbed `blocked_on_inputs` until verbatim capture with Gilbert (open item 5)**; write-off claims may progress to SALVAGE_BIDDING but not to SETTLEMENT until captured · `C-08` **payable** — registered now, status `blocked_on_inputs`, formula = top-priority CM capture (open item 5); consumed by T-01, T-13 and routing (§2.5); both templates render `PENDING CAPTURE` in this slot and **refuse sign-off** until the calc is live. (C-04 ❓ from discovery log.)

### 2.4 Template engine

Jinja2, `StrictUndefined` (any missing variable = render failure, never a blank). Registry `{id, version, channel: email|pdf|field_set, body_ref, required_fields[], min_verification: extracted|human_verified, locale}` — `locale` default `en-KE`; English-only v1 confirmed; T-06r tone variants (`broker|client`) are distinct registered templates under the same locale — a template **refuses to render** if any required field is below its `min_verification` (this is how "no letter goes out with an unchecked number" is enforced structurally). Motor v1: T-01 approval note (structure in PRD-08), T-02 salvage bid letter (7 merge fields), T-03 >4M alert (16 fields), T-04 bank-discharge request, T-06 doc-pack request (conditional blocks per checklist), T-07 decline letter, T-05/T-08/T-09/T-10 ❓ verbatim capture (Q-06) — registry entries created now with `status: pending_capture`. Rendered artifacts stored to S3 + `template.rendered`.

### 2.5 Routing config

Authority matrix as pack data: `[{max: 100_000_00, role: asst_claims_manager}, {max: 700_000_00, role: claims_manager}, {max: 1_500_000_00, role: gm}, {max: 4_000_000_00, role: md}, {max: null, role: chairman, side_effects: [render T-03]}]`. Resolver: `route(amount) → role` used by PRD-08; matrix changes are pack-version changes.

**Routing amount (v1.1, binding):** `amount := settlement.payable` (C-08) when computed; **fallback `reserve.total`** (C-02) while C-08 is `blocked_on_inputs` — the fallback only ever routes *upward*, which is the safe failure mode.

**Band semantics (v1.1, binding):** inclusive upper bound — `amount ≤ band.max`. Exactly `100_000_00` → `asst_claims_manager`, matching the as-is "≤ 100K". Boundary tests in CI alongside R-05/R-08 (the matrix must also cover `[0, ∞)` with no gaps or overlaps — PRD-13 conformance check).

### 2.6 Acceptance

All 16 rule slots registered (❓ ones in `blocked_on_inputs`/`pending_capture`, visibly); golden tests per rule/calc from corpus cases incl. boundary values (estimate = excess exactly; quote = 50.0% PAV exactly — spec: R-05 fires on strictly-greater); rule change deploys as pack version bump with zero code release; `rule_runs`/`calc_runs` reconstruct any figure on any claim to inputs + versions.