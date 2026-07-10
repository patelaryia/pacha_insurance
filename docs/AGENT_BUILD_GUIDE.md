# AGENT BUILD GUIDE — read this first

You are a coding agent building the Pacha claims platform. This guide tells you how to read the document set, what order to build in, which invariants you must never violate, and what to do when something is underdetermined. It is binding.

## 1. Document set & precedence

Read in this order. On any conflict, the earlier document wins.

1. `Section_0_Shared_Engineering_Decisions_v1.1.md` — ED-1..ED-11. **Highest authority.**
2. `Section_0.5_Shared_Agent_Runtime_v1.1.md` — AR-1..AR-5. Binding on PRD-05–12.
3. `PRD-00` … `PRD-13` — build specs. Field lists, schemas, enums, and thresholds in these documents are **the spec**: implement exactly what is written, no more, no less.
4. `Full-System_Acceptance_Trial_v1.1.md` — the exit test. Every metric here must be computable from platform data you are building; if you notice a metric that your implementation cannot produce, that is a register item (§5).
5. `Phase_3_Sequence_and_Open_Items_v1.1.md` — the open-items register. Items marked ❓ or `blocked_on_inputs`/`pending_capture` are **known, deliberate gaps**: build the slot, the status, and the visible blocked state — never invent the missing value.

All documents are v1.1 and self-consistent. If you find a residual conflict, that is a register item, not a judgment call.

## 2. Build order

**Phase 1 (start now, this order):** PRD-00 → PRD-01 → PRD-02 → PRD-03 → PRD-04. PRD-00 is the substrate; nothing else compiles without it. PRD-03's grader/autonomy machinery must exist before any agent PRD ships, because AR-2 routes every side-effect through capability levels.

**Phase 2:** PRD-05 → PRD-06 → PRD-07 → PRD-08 → PRD-09 (paste-assist mode of PRD-09 ships in week 1 of Phase 2; RPA mode follows DG-1/DG-3 and click-path capture).

**Phase 3:** per the Phase 3 sequence document — PRD-10 → PRD-11 → PRD-12 (gated by GP-1) → PRD-13 continuous.

Pack scaffolding (PRD-13 repo format, build pipeline, conformance suite) is built alongside Phase 1 — PRD-01/02 consume pack artifacts from day one.

## 3. Non-negotiable invariants (CI-enforced; violating any of these fails review)

1. **Money = BIGINT KES cents everywhere (ED-8).** No floats in any Money-typed signature. `money_kes` parses shillings from documents and ×100 on commit. Literals use the `_00` cents convention.
2. **Append-only field writes (PRD-00).** No in-place updates to `claim_fields`. New version + supersession + event, one transaction. Agents never supersede `human_verified` (409).
3. **No provenance, no commit (PRD-01 §1.4).** A field cannot reach `extracted` without a resolved citation (anchor_text or vision_bbox mode).
4. **One choke point for side effects (AR-2).** Zero direct sends/writes outside `execute_or_stage`. CI greps for `graph_client.send` and adapter `.execute` outside the gate module; `notify/` is the only whitelisted path (AR-5).
5. **Never guess.** Ambiguous inbound match, ambiguous invoice line, ambiguous EFT, multiple endorsement candidates, missing rule input → `EXCEPTION` / `blocked_on_inputs` / review item. There is no code path that silently picks.
6. **Never blind-retry a write (PRD-09).** Uncertain external-system writes → `EXCEPTION{uncertain_write}`, human check.
7. **StrictUndefined everywhere it has an analogue.** Templates refuse to render on missing/under-verified fields; pack merge refuses on missing manifest items; T-01/T-13 refuse sign-off while C-08 is `blocked_on_inputs`.
8. **No payment execution exists.** CI asserts the adapter op registry contains no funds-transfer operation (PRD-12 acceptance 6). GP-1 gating on `settlement.*` promotion is tested in CI (403 `GATE_GP1_CLOSED`).
9. **The review-item type enum is closed (PRD-04 v1.1).** Seventeen types, exactly. New cases are `EXCEPTION` subtypes. Every type ships its four-part contract including a versioned resolution-payload JSON schema.
10. **Ledger is single-writer (PRD-00 v1.1).** All audit appends via the concurrency=1 queue. Nothing else writes `audit_ledger`.
11. **Autonomy ceilings are hard-coded constitution:** `triage.ex_gratia` = L1 permanent; `triage.decline_draft` release = human; consistency flags (CC-5 class) = L2; `pack.note_draft` sign = human, max L3; `salvage.award` is not a capability; no L4 anywhere money-adjacent; approval authority is not a capability at all.
12. **Portal isolation (PRD-11).** The `lot_public` projection whitelist is the only data crossing the portal boundary; the insured-name-grep test must pass on every portal response.

## 4. Conventions

- ULIDs for all IDs. UTC storage, EAT rendering. British English in generated prose.
- Monorepo layout per ED-1: `/platform`, `/agents`, `/packs`, `/console`, `/infra`. Each PRD = one Python package with a public interface; cross-package imports go through those interfaces only.
- Every schema in the PRDs is DDL-level spec: column names, types, and comments are binding. Add columns only via a register item.
- Config over code: model IDs (ED-4a), LLM budgets (AR-4a), sampling rates, SLA definitions, thresholds, click-paths, templates, rules — all data. If you are about to hard-code a value that plausibly belongs to the pack, it belongs to the pack.
- Definition of done per ED-7/ED-7a: coverage boundaries, integration test per acceptance scenario, migration reviewed, OpenAPI generated, runbook page, grader coverage registered.
- Acceptance scenarios in each PRD are the integration test suite — implement them verbatim, including the negative/boundary cases (estimate = excess exactly; quote = 50.0% PAV exactly, R-05 strictly-greater; 50,000 routes desk, 50,001 physical; band boundary 100_000_00 inclusive).

## 5. Ambiguity protocol (ED-11 — binding)

If anything is underdetermined — a missing value, a conflict, an unstated behaviour:

1. **Do not decide locally.**
2. Implement the narrowest safe behaviour consistent with the platform doctrine: `blocked_on_inputs` for rules/calcs, `EXCEPTION` review item for runtime ambiguity, refuse-to-render for templates/packs.
3. Append an entry to the open-items register (`Phase_3_Sequence_and_Open_Items_v1.1.md`) describing the gap, the file/section, and the safe behaviour you implemented.

The register, not the codebase, is where ambiguity goes to die.

## 6. Known deliberate gaps (build the slot, not the value)

| Gap | Status you build | Unblocked by |
|---|---|---|
| C-08 payable formula | calc registered, `blocked_on_inputs`; T-01/T-13 render `PENDING CAPTURE` + refuse sign; routing falls back to `reserve.total` | open item 5 (top priority) |
| R-06 desk/physical threshold | rule `blocked_on_inputs`; MODE_CONFIRM goes to officer undetermined (their choices are labelled training data); `assessment.mode_confirm` capped L2 | open item 5 |
| C-07 both variants | `blocked_on_inputs`; write-off claims stop at SALVAGE_BIDDING→…→ but not SETTLEMENT | open item 5 |
| R-01/R-04/R-16, R-11 conditions | rule slots registered, `blocked_on_inputs`, visibly | open item 4 |
| T-05/T-08/T-08b/T-09/T-10/T-11/T-12/T-13/T-02b verbatims | registry entries `pending_capture` | open item 6 |
| `icon.reserve_adjust` click-path | op registered `pending_capture`; matched_under prompts officer-manual console task | open item 17 |
| ICON test instance | if none: first 25 RPA runs on real claims at L2 with officer watching (the ladder already requires this) | open item 2 |
| Runner host (on-prem vs Fargate) | build the outbound-only container per ED-3a; host-agnostic | open item 12 (DG-3) |
| Bidder KYC depth / bond / quorum | defaults per PRD-11 (2-of-3 quorum); config slots | open item 9 |
| DV channel / statutory clock | config slots, `pack config` defaults per PRD-12 | open item 10 |

## 7. v1.1 change log (what moved since v1.0, for reviewers)

- **ED-8 money (cents end-to-end)** — corrected `money_kes`, R-12 literal, all fixtures.
- **ED-3a runner topology** — pessimistic-case outbound-only RPA container; DG-3 decides host only.
- **PRD-00:** complete FSM (24 states incl. WITHDRAWN/VOID; decline-from-anywhere with CM approval post-triage; EX_GRATIA_REVIEW = substatus); `external.*` fields canonical in `claim_fields`, `claims.external_refs` demoted to cache; write concurrency (optimistic retry + per-claim advisory lock, atomic batches); events `seq` + 5s watermark replay; single-writer hash-chain ledger + audit-degraded mode; SLA `calendar` attribute; `dek_wrapped`, `value_search` blind index, `assigned_to`.
- **PRD-01:** DOC_SPLIT stage (v1.0 human boundaries, v1.1 agent proposals); `vision_bbox` citation mode (×0.9 confidence; handwritten forms expected in FIELD_VERIFY); Swahili structured-fields-in-scope + gloss never a rule input; `kenya_reg` full pattern set; page renders 180d.
- **PRD-02:** routing amount = C-08 payable, fallback reserve.total (routes upward); inclusive band bounds; C-08 registered; `effective_from` demoted to documentation; template `locale`.
- **PRD-03:** L1→L2 literal consecutive-25 with hard reset; L2→L3/L3→L4 rolling windows; deterministic sampling `sha256(run_id) % 100 < rate`; SAMPLE_REVIEW type; sampled edits count toward demotion.
- **PRD-04:** closed 17-type review enum + four-part contract; `head_of_claims` role (no default band, open item 13); owner-routed queues + pool view; notify-module exemption; keyboard roving-focus rules.
- **PRD-05:** final 5-class classifier contract (mail never silently dropped; not_a_claim ≥0.95 auto-archives with 10% sampling); §5.8 weighted round-robin assignment; closed-claim dupe → FRAUD_SIGNAL; terminal-state inbound handling + REOPEN_PROMPT.
- **PRD-06:** items.yaml registry (physical/field_request kinds — 422 path closed); per-checklist 48h deferral; suppression list extended to WITHDRAWN/VOID; send-window compliance.
- **PRD-07:** corrupted worked example struck; canonical fixture **FX-1** + billable-header/evidence-line semantics; tiles sum header rows only.
- **PRD-08:** `pre_projection_pack` deleted (RESERVED = computed locally); manifest items 12–13 `source: projection_readback|upload`, non-waivable; payable = C-08; network-disabled Chromium HTML→PDF policy, byte-stable regeneration.
- **PRD-09:** ops added `icon.reserve_adjust` (pending_capture), `icon.assessor_payment_request` (not behind GP-1, L3 cap + permanent sampling); readback → canonical `external.icon.claim_no`; PASTE_READBACK_CHECK item; ED-3a runner reference.
- **PRD-10:** `repair.payment_ready` registered field = formal PRD-12 trigger; invoice ambiguity → EXCEPTION; matched_under officer-manual until reserve_adjust captured.
- **PRD-11:** bids append-only + partial unique + `in_counter`; login page, nothing between lots; 14d election deadline, never auto-surrender; 48h counter-expiry Beat job; nullable reserve_estimate → `no_baseline` recovery rows; WAF + ASVS-L1 pen test, zero high/critical; KYC_VERIFY item.
- **PRD-12:** EFT exact-amount, ±5 business days, claim_no wins, EFT_MATCH item; S1 trigger includes `repair.payment_ready`; assessor/towing payment scope note.
- **PRD-13:** items.yaml + holidays.yaml in the pack layout; sha256 signing per ED-10; pinning sole versioning authority.
- **Trial:** 8-week structure (4 ramp + 4 measured), counters accrue from ramp day 1, legacy stub import, re-key metric restated (≤1 paste-assist / 0 RPA), first-pass approval ≥95% with Reject-only rework definition.
- **Section 0.5:** AR-1a reaper (acks_late + 15-min heartbeat, 3 attempts); AR-3 scope + AR-3a send window; AR-3b Graph mechanics (poll authoritative); AR-4a full budget table + $8/day / $12 lifetime per-claim ceilings; AR-5 notify module.
