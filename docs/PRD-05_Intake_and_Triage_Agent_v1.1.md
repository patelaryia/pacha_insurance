## PRD-05 — Intake & Triage Agent (build spec)

> **v1.1** — incorporates the CTO decision round of 2026-07. Where this document conflicts with Section 0 (Shared Engineering Decisions) or Section 0.5 (Shared Agent Runtime), those files win. Anything underdetermined: follow ED-11 (never decide locally — route to the open-items register).

### 5.1 Purpose

Shared mailbox → structured claim → triage decision → acknowledgement + document request, in minutes. Attacks the 8-day acknowledgement lag. Zero core-system writes.

### 5.2 Email router (runs on every `email.received`)

Every inbound message is first routed, in strict order (first match wins):

1. **Thread match:** Graph `conversationId` ∈ `communications.thread_id` → attach to that claim, emit `document.received` per attachment, done (this is how chase replies, assessor reports, and DVs re-enter the system — PRD-06/07/12 subscribe downstream).
2. **Reference match:** claim number (ours or ICON's) or reg plate (validator `kenya_reg`) in subject/body matching exactly one open claim → attach, plus `INBOUND_ATTACHED` info event on the claim timeline.
3. **Ambiguous match:** reg/name matches >1 open claim → review item `EXCEPTION{type: ambiguous_inbound}` with candidate list; officer assigns. Never guess.
4. **Classification** — `MODEL_LIGHT` on remaining mail. **Final classifier contract (v1.1 — this table replaces all prior enum lists, including the §5.3 `multi_intimation` mention; the enum is exactly these five classes):**

| class | confidence | routing |
|---|---|---|
| `new_intimation` | ≥ 0.85 | intake flow (5.3) |
| `new_intimation` | < 0.85 | `DOC_CLASSIFY{subtype: mailbox_triage}` |
| `multi_intimation` | any | `EXCEPTION{type: multi_claim_email}` — officer splits |
| `claim_related_unmatched` | any | `DOC_CLASSIFY{subtype: mailbox_triage}` |
| `not_a_claim` (broking/premium/marketing/spam) | ≥ 0.95 | auto-archive label + line in daily digest + 10% `SAMPLE_REVIEW` |
| `not_a_claim` | < 0.95 | `DOC_CLASSIFY{subtype: mailbox_triage}` |
| `unclear` | any | `DOC_CLASSIFY{subtype: mailbox_triage}` |

**Mail is never silently dropped:** auto-archived items remain queryable and digest-listed. At launch a human confirms all non-obvious mail; capability `intake.mailbox_triage` climbs the ladder like everything else.

Idempotency: `graph_message_id` unique constraint; redeliveries are no-ops. Loop guard: never process mail sent by ourselves (from-address check) — hard-coded.

**Terminal-state inbound (v1.1):** a document/reply arriving for a claim in a terminal state is **always attach + timeline note + owner notification, never a state change**. Additionally: DECLINED + substantive new evidence → review item `REOPEN_PROMPT` (owner decides); SETTLED/CLOSED + money-relevant document (invoice, demand) → `EXCEPTION` routed to CM; WITHDRAWN behaves as DECLINED.

### 5.3 Intake flow (COP steps for capability `intake.claim_creation`)

```
S1 create_claim         claims row (lob=motor, pack pinned), FSM=INTIMATED,
                        SLA 'acknowledge' starts (clock def PRD-00 §0.5)
S2 ingest               email body → synthetic intimation_email document; each
                        attachment → documents row → PRD-01 pipeline
S3 populate             await document.extracted for intimation_email (+ any
                        instantly-classifiable attachments); parties created from
                        sender + extracted broker/insured; intimation.date = email
                        received ts (system source, conf n/a)
S4 dupe_check           (a) OPEN claims, same vehicle.reg AND loss.date ±3d →
                        EXCEPTION{type: possible_duplicate}, run pauses.
                        (b) v1.1: CLOSED claims, same reg + loss.date ±3d, within
                        365 days → NOT a pause: FRAUD_SIGNAL info flag on the claim,
                        surfaced in triage and in the approval note's verification
                        section.
S5 late_check           R-10 evaluate (blocked_on_inputs until loss.date verified
                        if extraction confidence < threshold)
S6 acknowledge          via AR-3: template T-06a (acknowledgement) — see 5.5
S7 checklist            instantiate chase checklist (hand-off event chase.init →
                        PRD-06); include conditional items per T-06 logic
S8 triage               coverage + excess flow (5.4)
```

`new_intimation` with **photos only, no narrative** (observed pattern): claim still created; S3 marks `loss.narrative` missing → the doc request (S7) includes "description of the incident" as a chase item. Multiple claims in one email (two regs, two losses): classifier returns `multi_intimation` per the §5.2 contract → `EXCEPTION{type: multi_claim_email}`, officer splits; do not auto-create two claims v1.

### 5.8 Claim assignment (v1.1 — new)

**Weighted round-robin auto-assignment at `claim.created`** (weight = open-claim count per officer), implemented as event consumer `assigner`, writes `claims.assigned_to`. Ownership = default routing for that claim's review items (owner-routed queues), with the "all items" pool view available to every officer (PRD-04 S-1); resolution always logs the actual actor. Reassignment is a console action (officer self-serve, or CM bulk), ledgered. **Outbound signature = owning officer's name over the shared-mailbox contact block** (feeds T-06a's signature merge field). Role-addressed escalations stay role-based. Daily digest = owned claims.

### 5.4 Coverage, premium & excess triage (capability `triage.coverage_check`)

Two operating modes, switched per-adapter-availability (PRD-09 exposes `icon.read` health):

- **Mode A — no ICON read (launch):** agent creates review item `FIELD_VERIFY{subtype: coverage_manual}` presenting a **coverage checklist card**: policy number (extracted), fields for the officer to key from ICON — `policy.sum_insured`, `policy.period_start/end`, `policy.endorsement_ref`, `policy.premium_paid (bool)`, `policy.excess_protector (bool)`. These land as `human` source fields. Target ≤ 90s — one ICON lookup, five keys. This is deliberate: the human does the _read_, the platform does everything after.
- **Mode B — ICON read live:** adapter fetches the same fields; endorsement selection rule = the endorsement whose period contains `loss.date` (mismatch or multiple candidates → EXCEPTION, never pick silently — the silent wrong-endorsement error from as-is 3.1 must be impossible).

Then, deterministically: `C-01` excess → `policy.excess_amount`; loss date within period else `EXCEPTION{type: out_of_cover}`; premium unpaid → `EXCEPTION{type: premium_unpaid}` (officer decision, pack note records outcome); `R-02` below-excess → **decline path**: render T-07 decline letter draft + attach client context (loss ratio, premium history — Mode A: officer-keyed optional fields; Mode B: fetched), create `DRAFT_RELEASE{subtype: decline}` at capability `triage.decline_draft`. Ex-gratia consideration is a distinct item `EX_GRATIA` routed to `claims_manager` per R-03 — capability `triage.ex_gratia`, **max_level L1 permanently**. Release of a decline transitions FSM → DECLINED (red-banner state, PRD-04).

### 5.5 Templates owned by this PRD

`T-06a` acknowledgement (merge: insured, reg, loss date, claim handler signature block, our reference) · `T-06` document request — conditional blocks compiled from checklist state: base 7 items {claim form, logbook copy, DL copy, KRA PIN, police abstract, repair estimate, photos}; police abstract block omitted when R-11 waiver conditions hold; each item rendered with "already received ✓" state so re-sends never re-ask for held documents. Both `min_verification: extracted` (no money figures present).

### 5.6 Capabilities registered

|capability|max|launch level|promotion notes|
|---|---|---|---|
|`intake.mailbox_triage`|L4|L1|routing only; misroute = major, not critical|
|`intake.claim_creation`|L4|L2|creation is reversible (claim void action exists)|
|`intake.acknowledge`|L4|L1 → L3 fast track (25 clean releases)|first thing to go autonomous|
|`intake.doc_request`|L4|L1|pairs with acknowledge|
|`triage.coverage_check`|L3|L1|Mode B only; Mode A is human by construction|
|`triage.decline_draft`|**L2**|L1|a decline is a customer-facing adverse decision; release always human|
|`triage.ex_gratia`|**L1**|L1|permanent ceiling|

### 5.7 Acceptance

(1) Synthetic intimation email → claim created, fields populated w/ citations, acknowledgement drafted, checklist live, all < 5 min wall-clock; (2) reply on an existing thread attaches to the right claim with zero new claims created across 50-email replay; (3) below-excess corpus case → decline draft + ex-gratia item, no acknowledgement-of-processing sent; (4) duplicate reg+date → pauses with exception; (5) self-sent mail ignored (loop test); (6) at `intake.acknowledge`=L3, ack lands in test inbox < 15 min from intimation with zero human touches. KPI wired to dashboard: intimation→acknowledgement (baseline 8 days; target < 15 min).