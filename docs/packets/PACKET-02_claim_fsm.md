# PACKET-02 — Claim lifecycle FSM (PRD-00 slice 2)

> **Status:** issued · **Builder:** Codex per `AGENTS.md` · **Reviewer:** CTO per `CLAUDE.md`
> **Source spec:** `docs/PRD-00_Canonical_Claim_Object_and_Event_Spine_v1.1.md` §0.4,
> §0.7, §0.8 scenario (5). Precedence as ever: Section 0 → PRD-00 → this packet.
> **Depends on:** PACKET-01 (merged — `claim_core` field store, events, hydration).
> **Acceptance tests:** `tests/acceptance/test_packet_02_claim_fsm.py` — protected;
> failing by design until this packet is done.

## 1. Scope

**In:** the complete 24-state FSM with `ClaimStateMachine.transition()` as the single
enforcement point; `POST /claims/{id}/transition`; the `decline(reason)` action with
its approval slot; WITHDRAWN/VOID rules; `EX_GRATIA_REVIEW` substatus; terminal-state
semantics (suppression flags as data); structural guards that are buildable now;
`claim.status_changed` events; `blocked_reasons[]` in hydration; `external.icon.*` /
`external.edms.*` core-dictionary registration.

**Out (later packets):** rule/calc-linked guard *evaluation* (R-05, C-02/C-03,
"manifest complete", "officer signed" — PRD-02/03/08 machinery; see D-6); reopen
endpoint (PRD-05 REOPEN_PROMPT owns it; see D-7); SLA/chase suppression *consumers*
(PRD-06 packet reads the flags this packet ships); approval-item resolution wiring
(PRD-04 packet); `claims.external_refs` cache consumer.

## 2. Binding spec quotes (implement verbatim)

PRD-00 §0.4, state set:

> "**Complete state set (v1.1 — this list is exhaustive; agents implement exactly
> these):** `INTIMATED, TRIAGED, AWAITING_DOCS, IN_ASSESSMENT, REPORT_RECEIVED,
> REGISTERED, RESERVED, PACK_READY, IN_APPROVAL, APPROVED, IN_REPAIR, REINSPECTION,
> RELEASED, WRITE_OFF, SALVAGE_BIDDING, CLIENT_ELECTION, SURRENDER_CHECKLIST,
> RETAINED, SETTLEMENT, SETTLED, CLOSED, DECLINED, WITHDRAWN, VOID`"

PRD-00 §0.4, transitions (guards in parentheses; single enforcement point):

> ```
> INTIMATED → TRIAGED (coverage+excess evaluated)
> TRIAGED → AWAITING_DOCS
> AWAITING_DOCS → IN_ASSESSMENT (estimate received)
> IN_ASSESSMENT → REPORT_RECEIVED (assessor report parsed)
> REPORT_RECEIVED → WRITE_OFF (R-05 true) | REGISTERED (external.icon.claim_no captured)
> REGISTERED → RESERVED (C-02/C-03 EXECUTED LOCALLY with verified inputs —
>                         projection is a parallel tracker, NOT a guard; see PRD-08 §8.2)
> RESERVED → PACK_READY (manifest complete + note drafted)
> PACK_READY → IN_APPROVAL (officer signed note)
> IN_APPROVAL → APPROVED | (reject → PACK_READY with structured reasons)
> APPROVED → IN_REPAIR → REINSPECTION (R-08 routing) → RELEASED
> WRITE_OFF → SALVAGE_BIDDING → CLIENT_ELECTION → SURRENDER_CHECKLIST | RETAINED
> RELEASED | SURRENDER_CHECKLIST(complete, R-13/R-14 gate) | RETAINED → SETTLEMENT
> SETTLEMENT → SETTLED → CLOSED
> ```
>
> "Primary transitions … enforced in one place — `ClaimStateMachine.transition()` —
> everything else calls it."

PRD-00 §0.4, decline:

> "**Decline (action, not a single transition):** `decline(reason)` with reason enum
> `{below_excess, out_of_cover, fraud, non_disclosure, late_intimation, other}`.
> - From `TRIAGED`: standard path (R-02 etc.), officer releases per PRD-05.
> - From `{AWAITING_DOCS, IN_ASSESSMENT, REPORT_RECEIVED, REGISTERED, RESERVED,
>   PACK_READY}`: permitted, but **requires a `claims_manager` approval item** before
>   the transition commits.
> - `DECLINED` is terminal, reopenable. `EX_GRATIA_REVIEW` is a **substatus of
>   DECLINED** plus a review item — not a primary state."

PRD-00 §0.4, withdrawal/void + suppression:

> "- `WITHDRAWN`: reachable from any open state before `SETTLEMENT`; terminal,
>   reopenable (insured abandons the claim).
> - `VOID`: created-in-error only; permitted **only pre-REGISTERED** (no ICON record
>   exists). After registration, use `WITHDRAWN`.
> - All three terminals (`DECLINED, WITHDRAWN, VOID`) plus `SETTLED, CLOSED` suppress
>   all chase and SLA activity."

PRD-00 §0.4, parallel trackers:

> "`blocked_reasons[]` on the claim surfaces hard gates (e.g., `R-13: logbook not held`)."

PRD-00 §0.8, acceptance scenario covered:

> "(5) illegal FSM transition rejected with reason"

PRD-00 §0.2, external refs (dictionary entries this packet registers):

> "the core dictionary registers `external.icon.claim_no`, `external.icon.salvage_no`,
> `external.edms.*` as first-class field paths (source_type `projection_readback`,
> full provenance)."

## 3. Deliverable

Extend `platform/claim_core/` (same package):

- `claim_core/fsm.py` — `ClaimStateMachine` with the exhaustive state enum, the legal
  edge set exactly per §2, and `transition()` as the **only** code path that mutates
  `claims.status`/`substatus`. Everything (routes, decline, future agents) calls it.
- Each transition: status change + `claim.status_changed` event row
  (payload `{"from", "to", "reason"?, "guards_pending"?}`), one transaction,
  per-claim lock (PACKET-01 mechanics).
- State metadata **as data**: `is_terminal`, `suppresses_activity`
  (true for `DECLINED, WITHDRAWN, VOID, SETTLED, CLOSED`), `reopenable`
  (true for `DECLINED, WITHDRAWN`) — consumed by later packets.
- Dictionary additions: `external.icon.claim_no` (string), `external.icon.salvage_no`
  (string), `external.edms.folder_ref` (string), all `pii_class none`; writes require
  `source_type` ∈ {`projection_readback`, `human`} (D-8).

## 4. Guard model (D-6 — binding for this packet)

Two guard kinds in the transition table:

- **`structural`** — enforceable against data this packet can see; **enforced now**:
  - `REPORT_RECEIVED → REGISTERED`: current field `external.icon.claim_no` exists →
    else `409 TRANSITION_GUARD_BLOCKED`, body lists `{"blocked_on":
    ["external.icon.claim_no not captured"]}`.
  - `VOID`: only from pre-REGISTERED states (`INTIMATED, TRIAGED, AWAITING_DOCS,
    IN_ASSESSMENT, REPORT_RECEIVED`) → else 409.
  - `WITHDRAWN`: only from open states before `SETTLEMENT` (any non-terminal state
    except `SETTLEMENT, SETTLED, CLOSED`) → else 409.
  - `IN_APPROVAL → PACK_READY` (reject): request must carry
    `payload.reasons: [{code, detail}, ...]` non-empty → else `422
    REJECT_REASONS_REQUIRED` ("with structured reasons" is binding).
  - Terminal states: no outbound transitions (reopen excluded per D-7) → 409.
- **`rule_linked`** — depends on PRD-02/03/08 machinery not yet built (`coverage+excess
  evaluated`, `estimate received`, `assessor report parsed`, `R-05 true`,
  `C-02/C-03 executed`, `manifest complete + note drafted`, `officer signed`, `R-08
  routing`, `SURRENDER_CHECKLIST complete + R-13/R-14`): the edge is **open** to an
  explicit API call, and the transition event payload records
  `guards_pending: ["<guard text>"]` verbatim from §2. Rule wiring replaces these
  when the COP runtime lands. (Register #24.)

Illegal edge (not in the table at all) → `409 ILLEGAL_TRANSITION`, detail naming
current state, requested state, and the legal successors — scenario (5)'s
"rejected with reason".

## 5. API contract

- `POST /claims/{id}/transition` — body `{"to": "<STATE>", "payload"?: {...}}` →
  `200` hydrated-claim summary `{"id", "status", "substatus"}`.
  Errors: `ILLEGAL_TRANSITION` (409), `TRANSITION_GUARD_BLOCKED` (409, with
  `blocked_on` list), `UNKNOWN_STATE` (422), `REJECT_REASONS_REQUIRED` (422),
  `CLAIM_NOT_FOUND` (404). Unknown state string never guesses — 422.
- `POST /claims/{id}/decline` — body `{"reason": "<enum>"}`.
  - Invalid reason → `422 INVALID_DECLINE_REASON`.
  - From `TRIAGED` → `200`, status `DECLINED`, `claim.status_changed` event with
    `reason`.
  - From `{AWAITING_DOCS, IN_ASSESSMENT, REPORT_RECEIVED, REGISTERED, RESERVED,
    PACK_READY}` → **`202 {"code": "APPROVAL_REQUIRED"}`**, status unchanged,
    `review.created` event `{"type": "EXCEPTION", "subtype":
    "decline_approval_required", "reason": <reason>, "requested_by": <actor>}`,
    and the pending gate surfaces in `blocked_reasons` (D-5). Resolution wiring is
    the PRD-04 packet's job. (Register #23.)
  - From any other state → `409 ILLEGAL_TRANSITION`.
- `POST /claims/{id}/substatus` — body `{"substatus": "EX_GRATIA_REVIEW"}`: permitted
  only while status is `DECLINED` (else `409 SUBSTATUS_NOT_ALLOWED`); emits
  `review.created` `{"type": "EXCEPTION", "subtype": "ex_gratia_review"}` per §0.4
  ("plus a review item"). Clearing: `{"substatus": null}` always permitted on
  `DECLINED`. Any other substatus value → 422 (closed set for now; grows via
  register).
- `GET /claims/{id}` (extend PACKET-01 hydration) — add `"blocked_reasons": [...]`,
  derived, default `[]`; contains `"decline pending claims_manager approval"` while
  that gate is open (D-5).
- All routes take `X-Actor` per PACKET-01 D-3.

## 6. CTO decisions (D-x) and register entries

- **D-5** — `blocked_reasons[]` is a **derived** hydration property, not a column
  (§0.4 calls it a parallel tracker; §0.2 schema has no column and columns are
  register-gated). Sources this packet: open decline-approval gate. (No register
  entry needed — derivation is spec-consistent.)
- **D-6** — guard staging model per §4: structural guards enforced now, rule-linked
  guards open-with-`guards_pending` until PRD-02/03/08 land. (Register #24.)
- **D-7** — reopen: §0.4 says DECLINED/WITHDRAWN are "reopenable" but the target
  state and trigger live in PRD-05 (REOPEN_PROMPT). No reopen endpoint this packet;
  `claim.reopened` stays a registered event type; VOID is not reopenable
  (created-in-error). (Register #25.)
- **D-8** — `external.*` dictionary writes restricted to `source_type` ∈
  {`projection_readback`, `human`} — agents don't invent external refs; PRD-09
  readback and humans do. (Register #26.)

## 7. Definition of done (ED-7/ED-7a)

- All acceptance tests in `tests/acceptance/test_packet_02_claim_fsm.py` pass,
  unmodified; all PACKET-01 tests still pass.
- Unit tests ≥ 80% on `platform/claim_core/` (FSM edge matrix: cover every legal
  edge and a representative illegal edge per state).
- Alembic: no schema change expected; if one proves necessary it needs a register
  entry first.
- Gates green: `ruff check .`, `python3 tools/ci/money_float_lint.py`,
  `python3 tools/ci/banned_calls.py`, `python3 -m pytest -q`.
- Anything underdetermined: narrowest safe behaviour + register entry (ED-11).
