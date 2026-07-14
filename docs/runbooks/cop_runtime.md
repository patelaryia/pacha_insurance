# Runbook — COP runtime (PRD-02 slice 1, PACKET-06)

## Pack-load failures

Boot/load refuses the **entire pack** with `PackLoadError` — nothing partially
registers — for: malformed or meta-schema-invalid YAML, unknown rule keys,
unsupported or invalid JSONLogic, undeclared input aliases, unresolvable
claim/config/runtime input paths, duplicate pack versions or rule/calc ids,
invalid calc declarations, and calc sandbox violations (disallowed imports,
IO/eval, attribute access). Field-dictionary extensions register only after all
rule and calc checks succeed.

Recovery: fix the pack artifact, bump the pack version if the pack was ever
released (pinning is the sole versioning authority — PRD-13 §13.3), reload.

## Blocked-run triage

A `rule_runs`/`calc_runs` row with `status = blocked_on_inputs`:

1. Read `missing_inputs` — it names the exact unbound paths or capture slots
   (e.g. `formula.C-08`, `pack.late_days`).
2. Confirm the claim's pinned `pack@version` is loaded
   (`LookupError: PACK_VERSION_NOT_LOADED` otherwise).
3. Verify the current field exists at a committed verification state
   (`extracted` / `system_confirmed` / `human_verified`, plus any per-input
   `min_verification` floor).
4. Capture-gap slots (open items 4/5): the block clears only when the register
   item lands as a pack version bump — never by hand-editing a run row.

Blocked runs are auditable evidence, not retry signals. Operators must not
execute outcome data manually from these rows; outcome execution lands with
PRD-02 slice 2 behind the existing invariants.

## Render-refusal triage (PACKET-07)

`TemplateRenderBlocked` carries `reason` ∈ {`pending_capture`, `missing_fields`,
`under_verified`} plus `missing_fields` / `under_verified` path lists; for
`pending_capture`, the registry row's `blocked_on` names the open item. Never
substitute prose for a refused render. Calc-slot placeholders render the literal
`PENDING CAPTURE` and force `signable: false` — a non-signable artifact stays
non-signable until the calc goes live via pack version bump (open item 5).

## Guard-blocked transition triage (PACKET-07)

`409 TRANSITION_GUARD_BLOCKED` on a rule-wired edge (`REPORT_RECEIVED→WRITE_OFF`
R-05, `IN_REPAIR→REINSPECTION` R-08, `SURRENDER_CHECKLIST→SETTLEMENT` R-13/R-14):
`blocked_on` names the rule and its status; each guard check is itself recorded
in `rule_runs` — inspect the latest run's `missing_inputs`/`inputs_snapshot`.
Do not bypass blocked or non-fired rules. R-14 deliberately keeps SETTLEMENT
blocked until its captured inputs land (register #49/#64); this is the intended
v1 state, not an incident.

## Autonomy triage (PACKET-08)

**Auto-demotion:** every demotion writes `autonomy_changes` (reason
`auto_demotion`, evidence includes `trigger` + `trigger_event_id`) plus an
`ops.alert{autonomy_auto_demotion}` event. Investigate the triggering
`grader_runs` row (critical failure) or the rolling-20 resolution window
before considering re-promotion — re-promotion goes through the normal
signed API, never a DB edit. Demotions never occur below L1 automatically.

**Frozen promotions:** `platform_state['autonomy_promotions_frozen']` is set
by nightly ledger-verification failure (audit-degraded mode, PACKET-03).
Recovery: repair + re-verify the chain; the flag is cleared manually after
incident review. While set, every promotion returns 403 `PROMOTIONS_FROZEN`.

**Critical grader failure:** `review.created{EXCEPTION, grader_critical_fail}`
carries the grader id and subject_ref. Resolve the underlying data problem;
never resolve the item by re-grading until inputs changed. GP-1: `settlement.*`
promotions stay 403 `GATE_GP1_CLOSED` until `platform_state['gp1_open']` is
set by the GP-1 decision (PRD-12).

## Eval corpus triage (PACKET-09)

**Model-grader failure (G-CITE / G-NOTE):** `grader_runs.detail.code`
distinguishes model faults (`MODEL_GRADER_ERROR`, `CITATION_RENDER_ERROR` =
missing/corrupt page render, schema violations) from genuine grade fails
(`VALUE_NOT_PRESENT`, `EXACT_VALUE_MISMATCH`, `NUMERIC_TOKEN_OMISSION`,
`STRUCTURED_VALUE_MISMATCH`, `RUBRIC_FAILURE`). `error` results never pass and
never gate; investigate the render/blob/provider before re-grading. Model ids,
tiers, prompts and per-call budgets live in `packs/motor/eval/harness.yaml` —
config change only, never code (ED-4).

**Corpus vs production isolation (register #87):** grader runs carrying
`test_case_id` are corpus replays. They never create review items, never feed
promotion/demotion evidence windows, and their `grader.failed` events are
ignored by the autonomy consumer. If a demotion cites a corpus run, that is a
defect — do not "fix" it by re-promoting; file it.

**Blocked correction capture:** a `production_correction` test case with
`expected._capture.status="blocked_on_inputs"` names its `missing_inputs`
(capability, typed paths, current fields, corrected prose ref, pack pin).
The batch runner counts it blocked, never grades it. Recovery is upstream:
fix the producing `review.resolved` payload; the consumer is idempotent on
the source event id, so a corrected re-emission creates a new complete case.

**Weekly batch failure:** the Beat task `eval_harness.run_weekly_corpus` calls
the same synchronous `harness.corpus.run(...)`; reproduce locally with no
broker. Executor exceptions score as `errors` per case (never passes); a
disabled run raises against `weekly.enabled` in `harness.yaml`. Scorecard
`pass_percent` uses exact arithmetic over passed/failed/errors/blocked.

**Anonymisation refusal:** the exporter is all-or-nothing (register #88).
Refusals name the surface: unclassified/ambiguous PII, non-scalar or unknown
`value_type`, binary/image input, free-text keys, missing
`PACHA_ANONYMISATION_SECRET`, or an existing output path. Fix the bundle's
classification and re-run; never bypass a single field. The pseudonym mapping
is process-memory only — there is nothing to recover or rotate besides the
runtime secret.
