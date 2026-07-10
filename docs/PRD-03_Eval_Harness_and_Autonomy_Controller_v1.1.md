## PRD-03 — Evaluation Harness & Autonomy Controller (build spec)

> **v1.1** — incorporates the CTO decision round of 2026-07. Where this document conflicts with Section 0 (Shared Engineering Decisions) or Section 0.5 (Shared Agent Runtime), those files win. Anything underdetermined: follow ED-11 (never decide locally — route to the open-items register).

### 3.1 Purpose

The promotion machinery: graders on every production output, a growing labelled corpus, and governed per-capability autonomy levels. Nothing gets more autonomous without passing through this system.

### 3.2 Data model

sql

```sql
CREATE TABLE test_cases (
  id TEXT PRIMARY KEY, corpus TEXT NOT NULL,          -- 'motor_v1'
  origin TEXT NOT NULL,                               -- 'seed_closed_claim'|'production_correction'
  input_bundle JSONB NOT NULL,                        -- S3 refs to docs/emails (anonymised for seed)
  expected JSONB NOT NULL,                            -- {fields:{path:value}, rules:{id:fired},
                                                      --  calcs:{id:output}, note_rubric:{...}}
  tags TEXT[], created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE grader_runs (
  id TEXT PRIMARY KEY, grader_id TEXT, subject_type TEXT,  -- 'field'|'rule'|'calc'|'artifact'
  subject_ref JSONB, claim_id TEXT, test_case_id TEXT,     -- one of claim/test populated
  result TEXT NOT NULL,                                    -- 'pass'|'fail'|'error'
  severity TEXT NOT NULL,                                  -- 'critical'|'major'|'minor'
  detail JSONB, occurred_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE capabilities (
  id TEXT PRIMARY KEY,                                -- e.g. 'intake.acknowledge','pack.note_draft'
  current_level TEXT NOT NULL DEFAULT 'L1',           -- L0..L4
  max_level TEXT NOT NULL,                            -- hard ceiling, e.g. consistency checks = 'L2'
  policy JSONB NOT NULL                               -- promotion policy, 3.4
);
CREATE TABLE autonomy_changes (
  id TEXT PRIMARY KEY, capability_id TEXT, from_level TEXT, to_level TEXT,
  reason TEXT NOT NULL,                               -- 'promotion'|'auto_demotion'|'manual'
  evidence JSONB NOT NULL,                            -- run counts, pass rates, window
  approved_by TEXT, occurred_at TIMESTAMPTZ NOT NULL
);
```

### 3.3 Grader catalog (v1 — each a registered class with `grade(subject) → result`)

|Grader|Applies to|Logic|Severity|
|---|---|---|---|
|`G-CITE`|every extracted field|render cited bbox crop → MODEL_LIGHT verifies value present; money/date compared exact after parse|critical|
|`G-VAL`|fields|named validator re-run (belt-and-braces vs 1.5)|critical|
|`G-CALC`|calc runs|independent re-execution from claim inputs; byte-equal output|critical|
|`G-RULE`|rule runs|re-evaluate compiled JSONLogic against inputs_snapshot|critical|
|`G-SUM`|reserve/estimate|Σ line invariants|critical|
|`G-TPL`|rendered artifacts|all required fields present, no StrictUndefined leaks, verification floor met|critical|
|`G-NOTE`|approval-note prose|MODEL_HEAVY rubric: every numeric claim in prose matches a structured field (extract-and-compare); no unsupported assertions; tone/sections per T-01|major|
|`G-COMM`|outbound emails|recipient ∈ claim parties; template match; no unverified money figures|critical|
|`G-PROC`|agent runs|step sequence conforms to the capability's COP definition|major|

**Production gating rule:** `critical` fail ⇒ output blocked, review item created, `grader.failed` emitted. `major` fail ⇒ output allowed at L1/L2 (human will see it) but blocks at L3+.

### 3.4 Autonomy controller

Levels: L0 shadow (compute, don't surface) · L1 draft (human releases) · L2 one-click confirm · L3 auto-execute + N% sampled review · L4 straight-through. **Default promotion policy** (per capability, overridable in pack): L1→L2: ≥25 consecutive production items approved without material edit ∧ grader pass ≥96% ∧ Claims-Manager sign-off in console. L2→L3: ≥50 items, pass ≥98%, zero critical fails in window, CM sign-off; sampling starts at 20%, floor 5%. L3→L4: ≥100 items, ≥99%, zero critical in 60 days, CM **and** MD sign-off. **Auto-demotion:** any critical grader failure at L3+ ⇒ drop one level immediately + alert; rolling-20 pass <95% ⇒ drop one level. All changes ledgered with evidence snapshots. `max_level` ceilings hard-code the constitution: `assessment.consistency_flag`=L2, `triage.ex_gratia`=L1, anything touching approval authority = not a capability at all.

**Material edit definition (needed for "approved without edit"):** any change to a money/date/party/enum field value, or >15% token-level change to generated prose. Formatting-only edits don't count. Implemented as a structured diff on the review-resolution payload.

**Counter semantics (v1.1, binding):**
- **L1→L2 is literal consecutive-25:** any material edit or reject **resets the counter to zero**. (Harsh is correct when items are cheap and the level is low.)
- **L2→L3 and L3→L4 are rolling-window pass rates:** window = the stated item count (50 / 100); rejects and material edits both count as failures; formatting-only edits count as passes. "Zero critical fails in window" unchanged.

**Sampling mechanics at L3 (v1.1, binding):**
- Rate lives in `capabilities.policy.sampling_rate` (start 20%, floor 5% per the L2→L3 policy above).
- Selection is **deterministic**: `int(sha256(run_id)[:8], 16) % 100 < rate` — reproducible in tests and audits.
- Sampled items create review item type `SAMPLE_REVIEW` (added to the PRD-04 S-1 enum): the workspace renders the underlying item type with a "sampled — already executed" banner and the same resolution actions.
- A material edit on a sampled item **counts toward demotion statistics** — that is the point of sampling.

### 3.5 Correction capture loop

On every `review.resolved` where resolution ≠ `approve_unchanged`: auto-create a `test_case` (origin `production_correction`) pairing the input bundle with the human-corrected expected values; tag with capability + failure mode. Weekly batch eval re-runs full corpus per capability; results feed the promotion dashboard. Seed corpus: **≥100 anonymised closed claims** across the §3.6 strata (declines, standard repairs, betterment, late intimation, write-offs ± bank interest) — Aryia requests from Claims Manager; anonymisation script (names/IDs/phones → consistent pseudonyms, amounts preserved) is part of this PRD's deliverables.

### 3.6 Reporting API

`GET /eval/capabilities` (level, pass rates, runs-to-promotion), `GET /eval/corpus/stats`, `GET /eval/runs?capability=`, plus the four headline series PRD-04 charts: autonomy rate, no-touch rate, accuracy by capability, median review time. These are simultaneously the internal quality system and the investor/Mayfair traction pack — build them as first-class, not afterthought queries.

### 3.7 Acceptance

Every PRD-01/02 output type has ≥1 critical grader registered (CI check enforces); simulated critical failure at L3 demotes within one event cycle and pages; promotion is impossible via API without the sign-off record (403 otherwise); corpus batch run of 100 cases completes < 30 min with per-capability scorecards.