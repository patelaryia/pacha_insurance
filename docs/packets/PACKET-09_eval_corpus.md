# PACKET-09 — Model graders, correction corpus, batch evaluation & anonymisation (PRD-03 slice 2 of 2)

> **Status:** issued · **Builder:** Codex per `AGENTS.md` · **Reviewer:** CTO per `CLAUDE.md`
> **Source spec:** `docs/PRD-03_Eval_Harness_and_Autonomy_Controller_v1.1.md`
> §3.3 (`G-CITE`, `G-NOTE`), §3.5, §3.7 (corpus batch); PRD-01 §1.7;
> Section 0 ED-3/ED-4/ED-4a/ED-6/ED-11; Section 0.5 AR-4/AR-4a.
> Precedence: Section 0 → Section 0.5 → PRD-03/PRD-01 → this packet.
> **Depends on:** PACKET-08 merged on main (PRD-03 slice 1, PR #8).
> **Acceptance tests:** `tests/acceptance/test_packet_09_eval_corpus.py` —
> protected, failing by design until this packet is built.
> **Packet 10 (next):** PRD-04 status console & closed 17-type review queue.

## 1. Scope

**In:**

1. Flip **`G-CITE`** and **`G-NOTE`** from `pending` to `live`; leave
   `G-COMM`/`G-PROC` pending. Both model graders call through the existing
   `doc_intel.llm.ModelWrapper` ED-4a seam. Tests inject a fake `ModelClient`;
   the protected suite makes no network or live-provider call.
2. **`G-CITE`** (`field`, critical): resolve the current extracted field's
   citation, load the existing page render, crop the cited bbox, ask
   `MODEL_LIGHT` whether the value is present, and independently exact-compare
   money/date observations after parsing.
3. **`G-NOTE`** (`artifact`, major): load approval-note prose, ask
   `MODEL_HEAVY` for the configured T-01 rubric result and numeric-claim
   extraction, then independently compare every returned money/date claim with
   the named current structured field. Unsupported assertions, a missing
   configured section, bad tone, an unresolvable field, or an unparseable/
   mismatched numeric claim fails the grade.
4. **Correction capture:** idempotent dispatcher consumer on
   `review.resolved`; interim `resolution="approved"` is the register-#72
   spelling of `approve_unchanged` and creates no case. `edited`/`rejected`
   creates one `test_cases` row (`origin="production_correction"`) tagged with
   capability and failure mode. Typed paths are read from `diff.typed_changes`
   and the corrected expected values are hydrated from the current append-only
   field versions after the human write. Missing correction evidence produces
   a visible `blocked_on_inputs` case, never an invented label (register #80).
5. **Corpus service + batch runner:** create/read corpus cases, run the full
   selected corpus through a pinned `CorpusExecutor` seam, grade every returned
   subject, persist `grader_runs.test_case_id`, and return per-capability
   scorecards. Unsupported/blocked case bundles are counted as blocked and are
   never counted as passes (register #82). The real replay adapter plugs into
   the same seam when the seed-corpus manifest lands.
6. **Weekly execution:** one named Celery task with seven-day interval config in
   the motor pack and a synchronous engine method used by tests/operations. No
   broker is required to drive acceptance (D-9 posture).
7. **Anonymisation:** a callable + CLI for structured corpus exports. Names,
   IDs and phones become deterministic, claim-scoped keyed pseudonyms; money
   amounts remain byte-for-byte integers; the key and the in-memory mapping are
   never written. Unsafe/unclassified inputs are refused (register #85).
8. `/eval/corpus/stats` is exercised against cases created by the correction
   consumer and corpus service (no longer empty-only). Fix the PACKET-08 ratchet:
   autonomy pass percentages are not floor-divided; comparisons use exact
   numerator/denominator arithmetic.

**Out / blocked:** the actual ≥100-claim seed corpus (master item 7); the
`<30 min / 100 cases` live timing gate; PRD-01 §1.7 corpus accuracy gates
(97/98/95/99%); `G-COMM` (PRD-06 producer) and `G-PROC` (agent runs + PRD-13
COP definitions); PRD-01 monthly threshold calibration; PRD-04 UI/RBAC/review
tables; any production-data export to dev/staging. Synthetic fixture corpora
prove orchestration and scorecards only (registers #83/#84).

## 2. Binding spec quotes (implement verbatim)

PRD-03 §3.3:

> |`G-CITE`|every extracted field|render cited bbox crop → MODEL_LIGHT verifies value present; money/date compared exact after parse|critical|

> |`G-NOTE`|approval-note prose|MODEL_HEAVY rubric: every numeric claim in prose matches a structured field (extract-and-compare); no unsupported assertions; tone/sections per T-01|major|

> "**Production gating rule:** `critical` fail ⇒ output blocked, review item
> created, `grader.failed` emitted. `major` fail ⇒ output allowed at L1/L2
> (human will see it) but blocks at L3+."

PRD-03 §3.5:

> "On every `review.resolved` where resolution ≠ `approve_unchanged`:
> auto-create a `test_case` (origin `production_correction`) pairing the input
> bundle with the human-corrected expected values; tag with capability + failure
> mode. Weekly batch eval re-runs full corpus per capability; results feed the
> promotion dashboard. Seed corpus: **≥100 anonymised closed claims** across the
> §3.6 strata (declines, standard repairs, betterment, late intimation,
> write-offs ± bank interest) — Aryia requests from Claims Manager;
> anonymisation script (names/IDs/phones → consistent pseudonyms, amounts
> preserved) is part of this PRD's deliverables."

PRD-03 §3.7:

> "corpus batch run of 100 cases completes < 30 min with per-capability
> scorecards."

PRD-01 §1.7:

> "Against the PRD-03 motor corpus: classification accuracy ≥ 97%;
> financial-field extraction ≥ 98%, all fields ≥ 95%; citation resolution ≥ 99%
> of committed fields; handwritten claim-form per-field accuracy baseline
> measured in week 1 and threshold set with Claims Manager (A-4/Q-07 discharge);
> zero fields committed without provenance."

Section 0 ED-3/ED-4:

> "**Production PII never leaves prod** — dev/staging run on the synthetic +
> anonymised corpus only."

> "Model IDs live in config, never in code — swapping models must be a config
> change."

## 3. Deliverable

```text
platform/eval_harness/
  __init__.py       # injected ModelClient/CorpusExecutor; public exports
  graders.py        # G-CITE/G-NOTE live; field/T-01 consumer wiring
  corpus.py         # correction consumer, case service, batch + scorecards
  tasks.py          # named weekly Celery task; synchronous engine underneath
  anonymise.py      # fail-closed structured anonymiser + CLI entry function
  autonomy.py       # exact percentage ratchet (no floor division)
  api.py            # existing stats endpoint exercised on real rows
packs/motor/eval/harness.yaml
                    # model purposes/rubric refs + seven-day schedule; data only
```

No DDL change: PACKET-08's binding `test_cases`/`grader_runs` tables already
contain every required column. No `claim_core` source change is authorised;
Celery registration is package-local and may import the shared public
`claim_core.celery_app` only.

### 3.1 Pinned public surface (acceptance relies on exactly this)

```python
from eval_harness import (
    CorpusBatchResult, CorpusObservation, build_eval_harness,
)

harness = build_eval_harness(
    app,
    model_client=fake_model_client,       # doc_intel.llm.ModelClient
    corpus_executor=fixture_executor,     # optional CorpusExecutor
)

harness.graders.get("G-CITE").status == "live"
harness.graders.get("G-NOTE").status == "live"
harness.grade("G-CITE", {"claim_id": claim_id, "path": path,
                           "capability_id": capability_id}, actor)
harness.grade("G-NOTE", {"claim_id": claim_id, "blob_key": blob_key,
                           "template_id": "T-01",
                           "capability_id": "pack.note_draft"}, actor)

case_id = harness.corpus.create_case(
    corpus="motor_v1",
    origin="seed_closed_claim" | "production_correction",
    input_bundle={...}, expected={...}, tags=[...],
)
result = harness.corpus.run(corpus="motor_v1", capability_id=None,
                            actor="agent:eval")
# CorpusBatchResult{corpus, total_cases, runnable_cases, blocked_cases,
#                   total_grades, elapsed_ms, scorecards}
# scorecards[capability_id] =
#   {cases, grades, passed, failed, errors, blocked, pass_percent}

class CorpusExecutor(Protocol):
    def execute(self, case) -> list[CorpusObservation]: ...
# CorpusObservation{capability_id, grader_id, subject_ref}
```

The named task is `eval_harness.run_weekly_corpus`. Its body calls the same
`harness.corpus.run(...)` synchronously; Beat only schedules it. Pack config
owns the interval, corpus id and enabled flag.

### 3.2 `G-CITE` contract

- Valid subject = current `claim_fields` row at `{claim_id, path}` with
  `source_type="extraction"`, `verification_state="extracted"`, and resolved
  `{document_id, page, bbox, anchor_text|vision_bbox}` provenance. Anything
  else ⇒ `error`, never pass.
- Load `pages/{document_id}/{page}.png` and crop with the existing
  `doc_intel.vision.crop_png`; a missing/corrupt render or invalid bbox ⇒
  `error`. Never ask the model to infer from the whole document.
- Call `ModelWrapper.structured_call` with tier `MODEL_LIGHT`, task
  `g_cite_verify`, the crop, the current value and value type. Structured data:
  `{value_present: bool, observed_value: scalar|null}`.
- Non-money/date passes iff `value_present is true`. Money/date additionally
  require `observed_value`: `money_kes` must parse to the exact current integer
  KES cents; ISO date parsing must equal the exact canonical date. Unparseable
  or unequal ⇒ fail. Model `value_present=false` ⇒ fail.
- `field.updated` extraction consumption runs both G-VAL and G-CITE, once each
  per field version. G-CITE critical failures keep PACKET-08 gating unchanged.

### 3.3 `G-NOTE` contract

- Valid subject = existing UTF-8 artifact at `blob_key`, claim id, and
  `template_id="T-01"`; missing/non-UTF-8 artifact or any other template ⇒
  `error`.
- Call `ModelWrapper.structured_call` with tier `MODEL_HEAVY`, task
  `g_note_grade`, the note and the pack-config rubric/required-section ids.
  Structured data:
  `{numeric_claims:[{text, field_path, observed_value, value_type}],
  unsupported_assertions:[str], missing_sections:[str], tone_ok: bool}`.
- The grader, not the model, hydrates each named current field and exact-compares
  money/date observations after parse. Unknown field paths, other numeric value
  types, parse errors or mismatches fail closed. Any unsupported assertion,
  missing configured section or `tone_ok=false` fails. Empty numeric claims are
  valid only when the prose itself contains no numeric token; deterministic
  detection rejects a model omission.
- Direct grading is live now. The eval consumer also invokes G-NOTE for future
  `template.rendered{template_id:T-01}` traffic; PACKET-09 does not invent or
  un-pend the T-01 template.

### 3.4 Correction capture + corpus runner

- Consumer name: `correction_capture`; it subscribes only to
  `review.resolved`, is synchronously drivable with `dispatch_once()`, and keys
  idempotency to the source event id.
- Register-#72 mapping: `approved` = PRD spelling `approve_unchanged` (no case);
  `edited`/`rejected` = capture. Unknown resolution ⇒ visible
  `blocked_on_inputs` case, never silently treated as approval or correction.
- `input_bundle` records source event id, claim id, pinned pack version and
  immutable document `{document_id, blob_ref, sha256}` references. It never
  copies document bytes into the database.
- `expected.fields` contains the current values for the distinct typed-change
  paths after human correction. Tags contain exactly
  `capability:<id>` and `failure_mode:<resolution>` plus typed kinds. If a
  capability, typed path, current field or corrected prose reference is absent,
  the row still captures the event with
  `expected._capture.status="blocked_on_inputs"` and named missing inputs; the
  batch runner counts it blocked and does not grade it.
- `CorpusExecutor` is the replay boundary left open by the absent seed manifest.
  The runner selects every case in the requested corpus/capability, invokes the
  executor once per runnable case, adds `test_case_id` + capability to every
  returned subject ref, calls the real graders, and aggregates exact counts.
  Executor failure ⇒ that case scores `errors`, not pass; no batch write touches
  claims, fields, events, rules, calcs or artifacts except through the executor's
  already-public isolated-fixture APIs.
- Graders retain the PACKET-08 no-feedback rule: only `grader_runs` and
  `grader.passed`/`grader.failed`; batch grades never emit production output
  events and never mutate a production table.

### 3.5 Anonymisation contract

- Callable:
  `anonymise_bundle(bundle, *, claim_key: str, secret: bytes) -> dict`.
  CLI reads one structured JSON bundle and writes a new path atomically; it
  refuses overwrite, missing secret, binaries/images and unclassified PII.
- Each PII-bearing value must declare `pii_kind ∈ {name,id,phone}`. Pseudonyms
  are category-preserving and derived with HMAC-SHA256 over
  `(claim_key, pii_kind, normalised_value)`. Same source inside one claim maps
  consistently; the same source in another claim does not correlate.
- The secret comes only from runtime environment/secret provider. Neither
  secret nor source→pseudonym mapping is returned, logged, persisted, or written
  beside the output. Mapping exists only in process memory for one claim.
- Integer `value_type="money"` values are copied exactly. Booleans are not
  integers for this rule. Unsupported PII kinds or ambiguous free text refuse
  the entire output; no partial anonymised file survives.

## 4. CTO decisions (D-x) and register entries

- **Register #80** — #72 says `approved|edited|rejected` while §3.5 says
  `approve_unchanged`; map `approved` to unchanged. For corrections, hydrate
  only named typed paths after the human write; incomplete/unknown payloads
  create a visible blocked capture, never a guessed label.
- **Register #81** — model response schemas and subject refs are unstated;
  §3.2/§3.3 pin the narrow contracts above. Prompts/rubric/model ids/budgets are
  pack config; deterministic exact comparison stays in code.
- **Register #82** — seed `input_bundle` replay manifest and capability mapping
  do not exist. Ship `CorpusExecutor`; unsupported real bundles are
  `blocked_on_inputs`, while synthetic executors prove complete orchestration
  and scorecards.
- **Register #83** — corpus item 7 absent, so the 100-case timing and PRD-01
  97/98/95/99 gates remain live-ops blocked. Do not encode synthetic results as
  discharge.
- **Register #84** — monthly calibration inputs, output schema/window and
  threshold-change authority are unstated. No monthly job; current thresholds
  remain pack data unchanged.
- **Register #85** — anonymisation key/mapping/storage mechanics unstated. Use
  claim-scoped keyed pseudonyms, never persist mapping, preserve integer money,
  and refuse unclassified/binary input.
- **Register #86** — no weekly wall-clock is stated. Use a pack-configured
  seven-day interval from Beat start; the synchronous task is always drivable.
  A wall-clock slot can replace the interval when operations supplies it.

## 5. Builder guardrails

- **No live LLM in acceptance.** Use only the injected `ModelClient` through
  `ModelWrapper`; no grader calls Anthropic SDK or a provider client directly.
- **No feedback loops / production mutation.** Graders and corpus aggregation
  write only `grader_runs` + grader events. They never write/supersede
  `claim_fields`, write rule/calc runs, render templates, or emit production
  output events.
- **No provenance shortcut.** G-CITE grades only a real current extracted field
  with a resolved citation and real page render; it cannot accept a value passed
  only in the subject ref.
- **No new review-item types.** Critical G-CITE failures use the existing
  `EXCEPTION{grader_critical_fail}` path; G-NOTE major semantics remain the
  PACKET-08 gate. The closed enum stays at 17.
- Money is integer KES cents. Model-reported money is parsed then exact-compared;
  no tolerance and no `float` money signature.
- Model ids, prices, prompts, rubrics, schedules and thresholds are config data,
  never literals. Test fake ids are test-only.
- Production PII never leaves prod. Anonymisation fails the whole export on any
  unsupported/unclassified PII surface and never emits a plaintext mapping.
- `G-COMM`/`G-PROC` remain pending. T-01 remains `pending_capture`; this packet
  does not invent approval-note prose or required sections.
- No new table/column/migration; no `claim_core` source change. `.github/`,
  `tools/ci/`, `pyproject.toml`, and protected acceptance files are untouched by
  the builder. All PACKET-01–08 suites keep passing unmodified.

## 6. Definition of done (ED-7/ED-7a)

- All tests in `tests/acceptance/test_packet_09_eval_corpus.py` pass unmodified;
  full suite green on SQLite and PostgreSQL legs.
- `G-CITE`/`G-NOTE` live; `G-COMM`/`G-PROC` still pending. Unit tests cover
  model schema/error paths, missing/corrupt crops, exact money/date mismatch,
  unsupported note assertions, model omission of numeric tokens, correction
  idempotency and blocked capture.
- Synthetic corpus integration proves full selection, executor invocation,
  `test_case_id` persistence, per-capability exact scorecards and error/blocked
  accounting. It does **not** claim the live 100-case/accuracy gates.
- Anonymiser unit coverage includes consistent same-claim pseudonyms,
  cross-claim unlinkability, names/IDs/phones, exact integer-money preservation,
  no mapping in output/logs, and whole-output refusal on unsafe input.
- Weekly named task exists, reads pack config and is synchronously drivable with
  no broker. `/eval/corpus/stats` reports correction + seed fixture rows.
- `_pass_percent`/grader percentage reporting is non-truncated and promotion/
  demotion threshold comparison uses exact numerator/denominator arithmetic.
- ≥80% coverage on changed `platform/eval_harness/`; ruff, money-float lint,
  banned-calls and pytest green. No migration required; existing OpenAPI route
  shape remains valid.
- Runbook content (model-grader failure, blocked correction capture, weekly
  batch failure, anonymisation refusal) is flagged in the PR description — CTO
  owns protected docs.
- ED-11: any further ambiguity ⇒ narrowest safe behaviour + proposed register
  entry; stop and flag before expanding this packet.
