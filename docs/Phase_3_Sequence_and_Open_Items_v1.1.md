## Phase 3 sequence

PRD-10 weeks 12–15 (one engineer; reuses PRD-07 dispatch + PRD-06 patterns heavily) → PRD-11 weeks 14–20 (the portal is the long pole: bidder onboarding + pen-test before first live lot) → PRD-12 weeks 18–22 build, **go-live strictly gated by GP-1**, realistically week 26+ → PRD-13 motor-pack hardening continuous, fire-pack test weeks 16–22 in parallel (per the R4 plan). Nothing here blocks the analyst-equivalence trial, which needs only R2 scope.

## Master open-items register (consolidated ❓ — all discovery/decision, none code-blocking)

| #   | Item                                                                                                                    | Blocks                         | Owner              |
| --- | ----------------------------------------------------------------------------------------------------------------------- | ------------------------------ | ------------------ |
| 1   | Shared mailbox + `svc-pacha-icon`/`svc-pacha-edms` service accounts (in writing, longest lead)                          | ED-5, RPA L2+                  | Aryia → Mayfair IT |
| 2   | DG-1: ICON/EDMS browser-accessibility confirmation; ICON test instance existence                                        | PRD-09 RPA mode                | Aryia + IT         |
| 3   | Click-path recording sessions (1 day × ICON, EDMS) + dropdown value-map screenshots                                     | RPA ops leaving L1             | Aryia + Gilbert    |
| 4   | Rule definitions R-01/R-04/R-16; R-06 threshold (Q-02); R-11 waiver conditions                                          | those rules only               | Aryia (embed)      |
| 5   | C-04; C-07 verbatim; **payable formula** (single highest-value capture — feeds T-01, T-13)                              | PRD-08 L2, PRD-12              | Aryia + CM         |
| 6   | Verbatim templates: T-05, T-08/T-08b, T-09, T-10, T-11, T-12, T-13, T-02b; duty-paid source; re-inspection fee schedule | per-template `pending_capture` | Aryia (embed)      |
| 7   | ≥100-claim anonymised corpus request per §3.6 strata                                                                    | L2 promotions anywhere         | Aryia → CM         |
| 8   | DG-2: EFT form origin/format (mailbox vs drive hook)                                                                    | PRD-12 S8                      | Aryia + Finance    |
| 9   | ODQ-8: bidder KYC depth, bond rules, committee quorum                                                                   | PRD-11 S6/S8 config            | Aryia + HOC        |
| 10  | DV channel (via broker vs direct); statutory settlement clock trigger/duration (legal confirm)                          | PRD-12 S1 config, reporting    | Aryia + legal      |
| 11  | ODPC processor registration + DPIA (incl. af-south-1 transfer + LLM ZDR documentation)                                  | production go-live only        | Aryia              || 12 | **DG-3:** network path AWS ↔ ICON/EDMS (internet + IP allowlist vs LAN-only → on-prem runner host per ED-3a) — **in the same written IT request as item 1** | runner host provisioning only (architecture is host-agnostic per ED-3a) | Aryia → Mayfair IT |
| 13 | HOC approval band: distinct role or ≡ CM at Mayfair | PRD-04 band config | Aryia + CM |
| 14 | Verify the 277,476 figure vs fixture FX-1's reconstruction (139,200 garage door line) with Gilbert | nothing (FX-1 is canonical regardless) | Aryia |
| 15 | Legacy open-book export from ICON (CSV) for ramp-day-0 stub import (trial doc v1.1) | trial ramp day 0 | Aryia + IT |
| 16 | Reg-plate pattern verification against the corpus, week 1 (PRD-01 `kenya_reg` v1.1 set) | thread-match accuracy tuning | Eng + corpus |
| 17 | `icon.reserve_adjust` click-path recording (append to the item-3 recording sessions) | PRD-10 matched_under automation | Aryia + Gilbert |

**v1.1 priority note on item 5:** three captures are prioritised above all template verbatims, in one CM + Gilbert sitting this week: (a) **C-08 payable formula** (feeds T-01, T-13, routing amount); (b) **R-06 threshold** (`assessment.mode_confirm` cannot leave L2 until it lands); (c) **C-07 verbatim, both variants** (write-off claims progress to SALVAGE_BIDDING but not SETTLEMENT until captured).

**v1.1 process rule (ED-11, binding on coding agents):** anything found underdetermined during implementation is routed to **this register** — implement the narrowest safe behaviour (`blocked_on_inputs` / `EXCEPTION` / refuse-to-render) and log the gap here. The register, not the codebase, is where ambiguity goes to die.
