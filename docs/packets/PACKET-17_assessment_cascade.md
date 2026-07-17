# PACKET-17 — Assessment orchestration, slice 2: report ingestion, cascade C1–C5, savings ledger (PRD-07 §7.5–§7.7, PRD-07 complete)

> **Status:** issued · **Builder:** Codex per `AGENTS.md` · **Reviewer:** CTO per `CLAUDE.md`
> **Source spec:** `docs/PRD-07_Assessment_Orchestration_Agent_v1.1.md` §7.5–§7.7
> (§7.2–§7.4 shipped in PACKET-16); PRD-01 §1.3 (assessor_report closed field
> set), §1.4 (no provenance, no commit), §1.6 (CC-1..CC-5); PRD-02 R-05/R-07,
> C-02/C-03/C-05; PRD-00 §0.4 (REPORT_RECEIVED/WRITE_OFF edges), §0.5
> (`assessor_turnaround` stop); PRD-04 §4.3 (closed enum, PROCEED_PARTIAL,
> CONSISTENCY_FLAG); PRD-05 §5.2 (inbound matching); Section 0.5 AR-1/AR-2;
> Section 0 ED-1/ED-8/ED-9/ED-11; guide §3.5/§3.11/§6;
> registers #48/#49/#55/#56/#58/#59/#64/#75/#111/#159/#164/#167/#182/#184/
> #187/#190/#191/#194/#199.
> Precedence: Section 0 → Section 0.5 → PRD-00/01/02/04/05/07 →
> PACKET-06..16 contracts → this packet.
> **Depends on:** PACKET-16 merged on main (registers #181–#199).
> **Acceptance:** `tests/acceptance/test_packet_17_assessment_cascade.py` —
> protected, failing by design until this packet is built.
> **Packet 18 (next):** PRD-08 approval pack generator (consumes
> `reserve.total`, the verification ledger, and the consistency results this
> packet produces).

## 0. Slice boundary

PACKET-16 ends at the staged T-11 dispatch: `assessor_turnaround` clocks
running, one `assessor_report` chase checklist per firm, and the registered
but unfired `assessment.report_received` stop event. This packet builds the
inbound half: assessor-reply attribution, the §7.5 cascade C1–C5 verbatim
(write-off gate, multi-assessor selection, reserves, savings, flags), the
§7.6 `savings_ledger` with FX-1 as the canonical fixture, the header-rows-only
MTD reporting semantics, and scenarios §7.7 (1), (2), (3), (5) — (4) shipped
with PACKET-16. PRD-07 is complete when this packet lands.

**Reality constraints carried visibly:** T-11 was never actually sent (item
1/#130/#157), so assessor replies cannot thread-match an outbound
conversation — they arrive via the PRD-05 reference-match/manual-triage paths
until the transport packet lands (proposed #201). The PRD-01 assessor_report
schema commits **only** `agreed_quote` and `pav` (§1.3 verbatim — bold
targets); `assessor_fee`, `supplier_lines`, `salvage_value`, `flags` are
extracted-not-committed, and per #167's precedent the closed schema is not
altered here: fees reach `claim_fields` only by officer keying, and C-02
stays visibly `blocked_on_inputs` until they do (proposed #203). R-06 (Q-02)
and C-04/C-07/C-08 remain blocked per open item 5.

## 1. Scope

**In:**

1. **Report attribution (`agents/assessment_agent/report.py`, proposed
   #200/#201).** Consumer on `document.extracted` for
   `doc_type='assessor_report'` on a claim with `assessment.dispatched`
   events:
   - Attribute the document to a dispatched assessor party by matching the
     source communication's sender address against the #187 assessor-party
     emails. Unattributable report → `EXCEPTION{report_unattributed}` (new
     EXCEPTION subtype, four-part contract; enum stays 17) and **no
     cascade** — never guessed (proposed #201).
   - On attribution: emit **`assessment.report_received`**
     `{claim_id, document_id, assessor_party_id, vendor_id}` — this is the
     registered PACKET-16 SLA stop event; the `assessor_turnaround` clock
     for exactly that assessor party stops (#191 `key_field`). The
     PACKET-15 matcher already moves the `assessor_report` checklist item
     `received→verified` and completes the checklist — no chase changes.
   - **Financial verification floor (§7.5 verbatim, proposed #202):** the
     cascade arms only when the report's committed `assessment.agreed_quote`
     (and `assessment.pav` when present) carries stored extraction
     confidence **≥ 0.90** or is `human_verified`. Below-floor commits wait
     for their FIELD_VERIFY resolution; the cascade re-arms on that
     `field.updated`.
2. **C2 selection — multi-assessor wait-all (§7.5 verbatim, proposed
   #204/#205/#210).** When `assessment.multi_mode` is true:
   - Selection is **pending** until every dispatched assessor party has an
     attributed report, or the officer approves proceed-with-received.
   - **Timeout = SLA breach:** consumer on `sla.breached` for
     `assessor_turnaround` while selection is pending → one idempotent
     **`PROCEED_PARTIAL`** review item (existing type/schema @1) listing
     received and outstanding firms. Approve → selection proceeds with
     received; reject → keep waiting (item re-issues on the next breach
     event only if none open).
   - **Selection:** lowest attributed `agreed_quote` wins (R-07 §7.5). The
     rule engine cannot express a cross-row array compare, so selection is
     a deterministic cascade step recording
     `assessment.selection_completed` with the R-07 citation; the R-07
     pack rule stays `blocked_on_inputs` unchanged (#49 posture, proposed
     #204). The chosen firm's `agreed_quote`/`pav` are committed as new
     append-only versions citing the chosen document (each report's own
     extraction commit already produced earlier versions — supersession is
     the mechanism, never in-place).
   - **Comparison artifact (§7.5 verbatim):** table of firm ×
     `{agreed_quote, pav, fee, flags}` built from each attributed
     document's persisted EXTRACT output; durable home = the
     `assessment.selection_completed` event payload (and the
     PROCEED_PARTIAL payload for the partial path); console rendering is
     PRD-04 scope (proposed #210).
   - **Officer override:** a later `human`-source `assessment.agreed_quote`
     version that differs from the selected value →
     `EXCEPTION{selection_overridden}` review item (§7.5 "EXCEPTION review
     if officer overrides"; new EXCEPTION subtype, proposed #204).
   - Single-assessor claims skip C2: the sole attributed report's committed
     values stand, and the cascade proceeds directly.
3. **C1 write-off gate + FSM (§7.5 verbatim, proposed #211).** On cascade
   arm (single) or selection (multi):
   - Transition `IN_ASSESSMENT → REPORT_RECEIVED` ("assessor report
     parsed" guard edge) — this cascade owns the hop.
   - Evaluate **R-05** via cop_runtime (integer-exact
     `quote×2 > min(pav, si)`, strictly greater — #53). True → the R-05
     `set_field` outcome writes `assessment.write_off_indicated=true` and
     the cascade transitions `REPORT_RECEIVED → WRITE_OFF` (hand-off
     PRD-11 — nothing further; SALVAGE_BIDDING mechanics are out).
     Blocked (missing pav/si) or false → claim stays `REPORT_RECEIVED`;
     blocked rule runs stay visible. `REPORT_RECEIVED → REGISTERED` stays
     guarded on `external.icon.claim_no` (PRD-09) — untouched.
4. **C3 reserves (§7.5 verbatim, proposed #203/#207/#208).**
   - Execute **C-02** via cop_runtime. Missing
     `assessment.assessor_fee`/`assessment.reinspection_fee` →
     `blocked_on_inputs` calc run, visible; the report's extracted fee is
     **not** auto-committed (PRD-01 closed targets, #167 precedent) and
     the vendor standard fee is **never** substituted (reports show
     non-standard fees — §7.2's own example is KES 10,000 against the
     6,380 standard). Officer keys the fees from the report in console;
     the cascade **re-attempts C3/C4 on `field.updated`** for either fee
     path (proposed #203).
   - On C-02 executed: write **`reserve.total`** (new pack money field,
     `source_type='calc'` citing the calc run — mirror of the
     PACKET-14 excess write, proposed #207); emit **`projection.requested`**
     `{claim_id, calc_run_id, reserve_total}` — registered, consumed by
     nothing until PRD-09 (proposed #208). G-CALC/G-SUM critical graders
     already cover calc runs.
   - Attempt **C-03**: stays `blocked_on_inputs` (no committed
     `assessment.supplier_lines` with payee party ids — #49/#58 posture
     unchanged); visible, never faked.
5. **C4 savings ledger (§7.6 DDL + semantics verbatim, proposed
   #200/#206).**
   - **`savings_ledger` table, §7.6 DDL verbatim** (id, claim_id, kind,
     baseline_amount, achieved_amount, stored generated `saving`, evidence
     JSONB NOT NULL, vendor_id, occurred_at) on `claim_core.Base`;
     migration `0013_savings_ledger`; rows never deleted (ED-9). SQLite
     leg computes `saving` per-dialect (generated column mirror of #21).
   - **Header row** per completed cascade: `kind='assessment_negotiation'`,
     baseline = committed `assessment.estimate_total`, achieved = committed
     `assessment.agreed_quote`, via **C-05 executed without supplier-line
     binding** (the calc is untouched; supplier data never reaches
     `claim_fields`, so C-05's aggregate path runs clean). Evidence =
     `{calc_run_id, citations: [estimate + agreed-quote field citations]}`.
     Header rows are the **billable** measure.
   - **Line rows** (`kind='supplier_substitution'`): decomposition
     **evidence**, built from the attributed document's persisted EXTRACT
     `supplier_lines` — per line with both `garage_price` and
     `supplier_price` present: baseline = garage_price, achieved =
     supplier_price, `vendor_id` **null** (no supplier vendor registry
     capture — the supplier *name* rides in evidence with the document
     citation), one row per complete line. Lines missing either price are
     listed under the header row's `evidence.incomplete_lines` — recorded,
     never fabricated (proposed #206 — this realises the half of #58 the
     report data actually supplies; C-03/C-05 calc semantics unchanged).
   - **No arithmetic invariant links lines to header** (§7.6 verbatim) —
     no reconciliation check may be added.
   - Idempotent: one header row per selection/cascade completion; ledger
     writes emit `savings.recorded` events through the single-writer
     queue.
   - **Reporting sums header rows only (§7.6 verbatim):** the existing
     `savings_mtd_ytd` series re-points from the PACKET-12 interim C-05
     calc-run sum to `savings_ledger` header rows (same `{mtd, ytd}`
     contract, EAT calendar; proposed #209). CSV unchanged.
6. **C5 flags (§7.5 verbatim).** The attributed report's extracted
   `flags[]`, plus any CONSISTENCY_FLAG-worthy CC results not already
   raised by the PRD-01 engine, surface as **CONSISTENCY_FLAG** review
   items (existing type; direct `review.created` emission per the
   chase-EXCEPTION precedent). Capability ceiling `assessment.
   consistency_flag` max L2 (existing pack row) — D-04 stays human;
   duplicate flags dedupe on (claim, document, flag).
7. **FX-1 canonical fixture (§7.6 verbatim).** The acceptance suite drives
   the FX-1 numbers end-to-end: header 26_100_000 → 13_627_600 (saving
   12_472_400), line 13_920_000 → 4_800_000 (saving 9_120_000, Kawama in
   evidence), MTD tile Σ header rows = 12_472_400 only. The 277,476 figure
   stays under item-14 re-verification — FX-1 is canonical regardless.

**Out / visibly blocked:** PRD-09 projection consumer (`projection.requested`
is fire-and-forget here); C-03 supplier breakdown (#49/#58 — payee ids
uncaptured); C-04/C-07/C-08 and R-06 threshold (open item 5); fee
extraction target paths (doc-schema spec round, #203 mirror #167);
`salvage_value` commit + all PRD-11 lot mechanics beyond the WRITE_OFF
transition; supplier vendor registry capture (line `vendor_id` stays null);
R-07 pack-rule activation (engine array support is a spec round);
reinspection lifecycle (PRD-10); corpus byte-match gates (§7.7's "closed
claim's actuals" needs open item 7 — FX-1 synthetic stands in, mirror
#36/#55); Graph transport (item 1); console rendering of the comparison
artifact (PRD-04).

## 2. Binding spec quotes (implement verbatim)

PRD-07 §7.5:

> "Assessor reply enters via PRD-05 thread-match → PRD-01 extracts
> `assessor_report` schema. On fields verified (financial fields ≥ 0.90 or
> human-verified):"

> "C1 write_off_gate   R-05 evaluate → true: FSM → WRITE_OFF (hand-off
> PRD-11)"

> "C2 selection        multi-assessor: wait-all (timeout = SLA breach →
> proceed-with-received via officer confirm); build comparison artifact
> (table: firm × {agreed_quote, pav, fee, flags}); R-07 selects lowest
> agreed_quote → EXCEPTION review if officer overrides"

> "C3 reserves         C-02, C-03 execute → reserve.* fields (G-CALC, G-SUM
> run); projection request emitted (PRD-09 consumes when live)"

> "C4 savings          C-05: header delta (estimate_total − agreed_quote) +
> per supplier_line delta → savings_ledger rows (7.6)"

> "C5 flags            assessor flags[] + CC-1..CC-5 results →
> CONSISTENCY_FLAG review items (capability ceiling L2 — D-04 stays human)"

PRD-07 §7.6:

> "Header rows (`kind='assessment_negotiation'`, baseline =
> `assessment.estimate_total`, achieved = `assessment.agreed_quote`) are the
> **billable** measure. Line rows (`kind='supplier_substitution'`, baseline
> = garage line price, achieved = supplier price) are decomposition
> **evidence**. Reporting sums **header rows only**. No arithmetic invariant
> links lines to header (labour deltas make them non-additive), so
> double-counting is structurally impossible."

> "Every row must carry citation-bearing evidence — this ledger is
> contract-billable, so it inherits critical grader `G-SUM` + citation
> checks."

PRD-07 §7.7 (this packet's scenarios):

> "(1) Corpus standard-repair claim end-to-end: estimate in → mode item →
> dispatch draft → (inject assessor reply) → report parsed, reserve
> computed, savings row written with citations, consistency flags raised —
> reserve figures byte-match the closed claim's actuals; (2) write-off
> corpus case crosses R-05 at exactly >50% (boundary test) → WRITE_OFF
> transition; (3) multi-assessor with one non-responder → SLA breach →
> proceed-with-received flow; (5) savings MTD tile reconciles to Σ **header
> rows only** (per §7.6 semantics), verified against FX-1."

## 3. Deliverable

```text
agents/assessment_agent/
  report.py       # attribution, report_received, verification floor
  cascade.py      # C1 write-off gate, C3 reserves + re-attempt, C4 ledger, C5 flags
  selection.py    # C2 wait-all, PROCEED_PARTIAL, R-07 selection, override watch
platform/claim_core/alembic/versions/0013_savings_ledger.py
docs/runbooks/assessment_cascade.md   # or extend assessment_agent.md
```

**Authorised existing-package changes, exactly these:**

1. `claim_core`: (a) `ledger.ACTION_MAP` gains `assessment.report_received`,
   `assessment.selection_completed`, `projection.requested`,
   `savings.recorded` (#209; precedent #170/#194); (b) migration
   `0013_savings_ledger`. Nothing else.
2. `doc_intel`: **one** curated public read,
   `extraction_output(document_id) -> dict | None`, returning the persisted
   EXTRACT stage output (the data already stored by `_store_output`) — the
   ED-1 door for supplier_lines/fee/flags without committing them
   (proposed #200; mirror of the #60 curated-surface rule). No pipeline
   change.
3. `review_queue`: `_savings` reader re-pointed to `savings_ledger` header
   rows (§1.5, same response contract). No other change.
4. `assessment_agent` (own package): PACKET-16 modules untouched except
   `build_assessment_agent` registering the three new consumers.
5. No `eval_harness` change: reserve/savings calc runs are already
   G-CALC/G-SUM critical-graded; CONSISTENCY_FLAG items ride the existing
   contract. Confirm grader coverage explicitly in the PR description.

`.github/`, `tools/ci/`, protected acceptance files untouched by the
builder. No new CI legs. Pack data edits, exactly these:
`packs/motor/fields.yaml` gains `reserve.total` (money, pii none — #207).
No doc-schema, template, autonomy, chase, or dashboard.yaml edits (the
`savings_mtd_ytd` row already exists and stays `live`).

### 3.1 Pinned public surface (acceptance relies on exactly this)

- Consumers registered by `build_assessment_agent` (names pinned):
  `assessment_report` (on `document.extracted`), `assessment_cascade`
  (on `field.updated`), `assessment_selection` (on `sla.breached` +
  `review.resolved`). Handle methods stay on `app.state.assessment_agent`.
- `assessment.report_received` payload:
  `{"claim_id", "document_id", "assessor_party_id", "vendor_id"}`; the
  matching `assessor_turnaround` clock row has `stopped_at` set; other
  firms' clocks keep running.
- Unattributed assessor_report (sender matches no assessor party) → open
  `EXCEPTION` review item, `subtype="report_unattributed"`, four-part
  payload; zero cascade side-effects.
- `savings_ledger` columns queryable exactly per §7.6 DDL; `saving` =
  `baseline_amount - achieved_amount` on both SQLite and PostgreSQL legs.
- Header row evidence: `{"calc_run_id": str, "citations": [...]}` with at
  least the agreed-quote citation; line row evidence carries the document
  id + supplier name; incomplete lines appear as
  `evidence["incomplete_lines"]` on the header row.
- `assessment.selection_completed` payload:
  `{"claim_id", "selected_party_id", "selected_document_id", "rule_id":
  "R-07", "comparison": [{"assessor_party_id", "vendor_id",
  "agreed_quote", "pav", "fee", "flags"}, …]}` — one entry per attributed
  firm; `fee`/`pav`/`flags` null/empty when the report lacked them.
- Multi-assessor + pending selection + `sla.breached{assessor_turnaround}`
  → exactly one open `PROCEED_PARTIAL` item; payload lists
  `received: [party ids]` and `outstanding: [party ids]`. Resolve approve
  with `PROCEED_PARTIAL@1` payload
  `{"capability_id": "assessment.selection", "diff": …}` (the schema's
  free-form capability string, pinned here) → selection completes with
  received firms. Reject → item closes, selection stays pending, a
  subsequent breach event re-issues.
- `reserve.total` committed with `source_type="calc"` and a `calc_run`
  source_ref; value = C-02 output exactly. `projection.requested` payload
  `{"claim_id", "calc_run_id", "reserve_total"}`.
- C-02 blocked path: `calc_runs` row `status='blocked_on_inputs'` naming
  the missing fee paths; after officer fee writes, a later executed C-02
  run exists and `reserve.total` commits — re-attempt is automatic on
  `field.updated` for `assessment.assessor_fee` /
  `assessment.reinspection_fee`.
- Write-off: R-05 true → `assessment.write_off_indicated=true` committed +
  claim status `WRITE_OFF`; boundary `quote×2 == min(pav, si)` → status
  stays `REPORT_RECEIVED`, no write-off field.
- Officer human-write of `assessment.agreed_quote` differing from the
  selected value → open `EXCEPTION` item `subtype="selection_overridden"`.
- `GET /console/ops/portfolio` `savings_mtd_ytd` data = `{"mtd", "ytd"}`
  summing **header rows only** from `savings_ledger` (EAT calendar).
- `packs/motor/fields.yaml` has `reserve.total` `{value_type: money,
  pii_class: none}`; `packs/motor/rules/R-07.yaml` still
  `status: blocked_on_inputs` (unchanged).
- Events + ACTION_MAP: `assessment.report_received`,
  `assessment.selection_completed`, `projection.requested`,
  `savings.recorded` all ledgered; `chase.complete` fires for the
  assessor_report checklist via existing machinery.

## 4. CTO decisions (D-x) and proposed register entries

Builder appends with the implementation PR; entries are **#200–#211**.

- **#200 — module + curated-read boundary.** Cascade lives in
  `agents/assessment_agent/{report,cascade,selection}.py`; migration
  `0013_savings_ledger`; doc_intel exposes one curated
  `extraction_output(document_id)` read of the already-persisted EXTRACT
  output (ED-1 door; mirror #60) — extracted-not-committed data becomes
  readable without ever touching `claim_fields`.
- **#201 — report arrival + attribution.** §7.5 says "thread-match", but
  item 1 means no T-11 was ever really sent, so replies arrive via
  PRD-05 reference-match/manual triage until the transport packet.
  Attribution = source-communication sender address ∈ the #187 assessor
  party's email; no match → `EXCEPTION{report_unattributed}`, no cascade —
  a report is never attributed by elimination.
- **#202 — "financial fields ≥ 0.90" realization.** The floor reads the
  stored extraction confidence on the committed version (or
  `human_verified`); below-floor commits leave the cascade un-armed until
  the FIELD_VERIFY resolution writes the human version. Stricter than the
  #56 default floor, scoped to this cascade only, per §7.5's explicit
  number.
- **#203 — fee commit gap (mirror #167).** PRD-01's closed target set
  commits only agreed_quote/pav; the extracted `assessor_fee` is visible
  in the EXTRACT output but never auto-committed, and the vendor standard
  fee is never substituted (observed reports differ from standard). C-02
  runs blocked-visible until the officer keys
  `assessment.assessor_fee`/`assessment.reinspection_fee`; the cascade
  re-attempts on those commits. Extraction target paths need a doc-schema
  spec round; at-report-time reinspection-fee semantics need capture.
- **#204 — R-07 realization + override watch.** The json-logic engine
  cannot express a cross-report array minimum; selection is a
  deterministic cascade step recording `assessment.selection_completed`
  citing R-07, tie-break on lowest quote then lowest party id; the R-07
  pack rule remains `blocked_on_inputs` unchanged. A subsequent differing
  `human` agreed_quote version raises `EXCEPTION{selection_overridden}` —
  the §7.5 override review; the human value stands (append-only; agents
  never supersede it back).
- **#205 — wait-all completion + partial confirm.** Completion = every
  dispatched assessor party attributed. Timeout realised as: `sla.breached`
  for `assessor_turnaround` with selection pending → one idempotent
  `PROCEED_PARTIAL` item (existing PRD-04 type); approve = proceed with
  received; reject = keep waiting, later breach events may re-issue. Zero
  received reports + full breach stays pending — chase escalation (cap-6)
  covers the dead-assessor path. The resolution's `capability_id` string
  `"assessment.selection"` is the schema-required label only — it is
  **not** a pack capability and gets no autonomy row (approval authority
  is not a capability, guide §3.11).
- **#206 — supplier line rows from extraction (partial #58 discharge).**
  The assessor_report `supplier_lines` carry `{garage_price,
  supplier_price}` — the operands #58 said were missing now exist as
  extracted document data. Line rows compute `garage_price −
  supplier_price` per complete line with document-citation evidence and
  null `vendor_id` (supplier names uncaptured as vendors); incomplete
  lines are listed in header evidence, never fabricated. Units: extracted
  line prices are shilling-denominated numerics (PRD-01 §1.3's own
  example) and take the `money_kes` normalisation (×100 to integer cents,
  ED-8) before any ledger write; a non-numeric price makes the line
  incomplete. C-05 and C-03 calc semantics are untouched — supplier data
  never reaches `claim_fields`.
- **#207 — `reserve.*` dictionary gap.** PRD-07 names `reserve.*` fields
  with no dictionary entries anywhere; `reserve.total` (money) is
  registered as pack data and written `source_type='calc'` from the C-02
  run (PACKET-14 excess precedent). Reserve *lines* land with C-03's
  unblock, not before.
- **#208 — `projection.requested` fire-and-forget.** Registered + ledgered
  now; PRD-09 registers the consumer. No retry/ack semantics until then.
- **#209 — savings series source swap + event mappings.** `savings_mtd_ytd`
  re-points from the PACKET-12 interim (Σ C-05 calc runs) to §7.6's
  binding rule (Σ `savings_ledger` header rows), same response shape.
  ACTION_MAP gains the four §3.1 events.
- **#210 — comparison-artifact home.** Durable in the
  `assessment.selection_completed` (and PROCEED_PARTIAL) payloads, built
  from persisted EXTRACT outputs at selection time; console rendering is
  PRD-04 scope.
- **#211 — cascade FSM ownership.** The cascade owns
  `IN_ASSESSMENT → REPORT_RECEIVED` on arm/selection and
  `REPORT_RECEIVED → WRITE_OFF` on R-05 true (both PRD-00 guard edges,
  event per hop, mirror #182); R-05 blocked-or-false leaves
  `REPORT_RECEIVED`; `REPORT_RECEIVED → REGISTERED` stays guarded on the
  ICON claim number (PRD-09) and is not touched.

## 5. Builder guardrails

- **Never guess** — no attribution by elimination; no fee substitution
  from vendor standards; no invented supplier vendor ids; incomplete
  lines recorded, not fabricated; blocked C-02/C-03/R-05 runs stay
  visible; selection never proceeds partially without the officer's
  PROCEED_PARTIAL approval.
- **No provenance, no commit** — every committed version (selected
  quote/pav, reserve.total, write_off_indicated, officer fees) carries
  source_ref; ledger evidence is citation-bearing on every row (§7.6 —
  contract-billable).
- **Money is integer cents end-to-end (ED-8)** — baseline/achieved/saving
  BIGINT; no float anywhere near `savings_ledger`; R-05 stays
  integer-exact.
- **Append-only** — ledger rows never deleted or updated; field writes are
  new versions; agents never supersede `human_verified` (409) — the
  override watch *observes* the human version, it never reverts it.
- **Closed enums** — review types stay 17 (`report_unattributed`,
  `selection_overridden` are EXCEPTION subtypes with the four-part
  contract; PROCEED_PARTIAL and CONSISTENCY_FLAG already exist); ledger
  `kind` exactly §7.6's three (only two produced here —
  `salvage_recovery` is PRD-11's).
- **Single-writer ledger** — `savings.recorded` and every audit row via
  the existing concurrency-1 writer; no second writer.
- **Idempotency** — consumers dedupe on event id; one header row per
  cascade completion; one PROCEED_PARTIAL open at a time; re-delivered
  `document.extracted`/`sla.breached` are no-ops; C-02 re-attempts create
  new calc runs, never mutate old ones.
- **Determinism** — selection tie-break pinned (#204); comparison rows
  ordered by party id; no RNG; cascade is a pure function of (db, event).
- **No payment execution** — reserves and savings are records; no
  funds-transfer op, no adapter registry entry (PRD-12 acc. 6).
- **Config over code** — `reserve.total` registration, series windows:
  pack data. Cascade sequence and FSM hops are spec logic citing §7.5.
- All PACKET-01–16 suites keep passing unmodified.

## 6. Definition of done (ED-7/ED-7a)

- `tests/acceptance/test_packet_17_assessment_cascade.py` passes
  unmodified on SQLite and PostgreSQL legs; full suite green — **zero
  owner-side fixture amendments this packet** (pre-checked: no protected
  pin collides; the savings tile pin is shape-only).
- §7.7 scenarios (1), (2), (3), (5) verbatim, including: R-05 boundary at
  exactly `quote×2 == min(pav, si)` staying non-write-off; FX-1 numbers
  exact (12_472_400 header / 9_120_000 line / MTD = header only);
  blocked-then-keyed fee path in scenario 1.
- ≥80% coverage on the new `assessment_agent` modules; migration `0013`
  reviewed (one table, generated column both dialects); OpenAPI unchanged
  (no new routes) — state so in the PR; runbook page (attribution
  triage, fee keying, partial-proceed flow, override exceptions, ledger
  audit); grader coverage confirmed (G-CALC/G-SUM on the new calc runs).
- ED-11: further ambiguity ⇒ narrowest safe behaviour + proposed register
  entry (next free number **#212**); stop and flag before expanding this
  packet.
