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
