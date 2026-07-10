## PRD-12 — Settlement & Finance Linkage (build spec)

> **v1.1** — incorporates the CTO decision round of 2026-07. Where this document conflicts with Section 0 (Shared Engineering Decisions) or Section 0.5 (Shared Agent Runtime), those files win. Anything underdetermined: follow ED-11 (never decide locally — route to the open-items register).

### 12.1 Purpose

The DV → EDMS → ICON voucher → payment-routing flow, plus EFT auto-attach. 100% `MECH`/`DET`, money-adjacent, therefore last and gated. Payment _execution_ and approval authority are permanently out of scope — the platform prepares and projects; humans and existing systems move money.

### 12.2 Gate protocol GP-1 (hard precondition, enforced in the autonomy controller)

All `settlement.*` and payment-adjacent `project.*` capabilities are frozen at L1/paste-assist until **all** of: (a) ≥ 90 days elapsed with ≥ 3 non-settlement capabilities sustained at L3+; (b) projection divergence rate = 0 over trailing 30 days; (c) two-person sign-off recorded in console (`claims_manager` **and** `md`, distinct PROMOTION_SIGNOFF items). The promotion API returns 403 with `GATE_GP1_CLOSED` otherwise — tested in CI. Post-gate ceilings: max **L3** on everything in this PRD; there is no L4 anywhere money-adjacent, permanently (sampling floor 10%).

### 12.3 Flow (capability set `settlement.*`)

```
S1 dv_issue     trigger: FSM=RELEASED AND repair.payment_ready=true (repair path,
                v1.1 formal contract per PRD-10 S7), or surrender-complete / retained
                (write-off). Render DV T-13 ❓verbatim capture: payee, amount =
                C-08 payable (single source, PRD-02 §2.3 — blocked_on_inputs until
                captured; DV issue blocks on it),
                R-14 override: payee = financing bank when discharge letter governs.
                Channel: via broker per current practice ❓confirm. DRAFT_RELEASE.
                SLA settlement_statutory starts at *signed* DV receipt (default
                90d, pack config — ❓legal confirm exact statutory trigger/duration
                under the Insurance Act before relying on it in reporting).
S2 dv_verify    inbound signed DV → PRD-01 discharge_voucher schema: signed=true
                (vision), amount == issued amount, payee == issued payee.
                Any mismatch → EXCEPTION{type: dv_mismatch} — never proceed.
S3 gate_check   FSM guard → SETTLEMENT re-evaluates R-13/R-14 (belt-and-braces).
S4 edms_blank   project edms.claim_payment: attach blank DV + note, submit.
S5 edms_signed  project edms.payment_workflow: attach signed DV — click-path
                includes the slow-reflection poll (30s interval, 10-min ceiling,
                then EXCEPTION) per PRD-09 known-failure handlers.
S6 voucher      project icon.payment_voucher: reserve → Action → General Payment
                → DV → generate → download → artifact saved as
                'Claim Payment Request {Reg}' (naming preserved) + attached to claim.
S7 edms_submit  project edms.attach_and_tag (voucher) + submit with comment →
                routes to approver per existing matrix. UNTOUCHED from here.
S8 confirm      payment confirmation detected via Finance hook (12.4) →
                FSM → SETTLED; salvage disposal/archival tasks close out; → CLOSED.
```

### 12.4 Finance linkage (EFT auto-attach) — resolves Q-12 behind one interface

`FinanceHook` interface with two launch-selectable implementations (decision **DG-2** at embed with Finance): (a) **mailbox hook** — Finance forwards/cc's EFT confirmations to a dedicated address (`claims-finance@` alias on the shared mailbox); PRD-01 schema `eft_form` {claim_no|reg, amount, payee, value_date, bank_ref} → **matching rules (v1.1, binding):** amount match is **exact**; value_date within **±5 business days**; precedence: an exact claim_no match wins outright; if claim_no and reg point at different claims, or two claims tie → review item type **`EFT_MATCH`**, always — never guess. Matched → attach + reconcile amount vs signed DV (mismatch → EXCEPTION); (b) **drive hook** — Graph delta watch on a Finance-shared folder, same parse path and matching rules. Ambiguous match → `EFT_MATCH` review item, never guess. Capability `finance.eft_attach`: max L4 (read-only attach + reconcile; the one settlement capability allowed full autonomy).

**Scope note (v1.1):** the assessor payment request is handled by `icon.assessor_payment_request` (PRD-09 registry — replicates an existing straight-through step, not behind GP-1, capped L3 with permanent sampling). Towing/supplier invoice **payment** stays in Finance's existing manual process v1 — the platform's role ends at the matched invoice (PRD-10 S7).

### 12.5 Acceptance

(1) GP-1 enforcement: promotion attempt pre-gate → 403; post-gate with one signature → still 403; (2) corpus settled claim replays S1–S8 end-to-end in staging with paste-assist, voucher artifact named to convention; (3) signed DV with amount off by KES 1 → dv_mismatch exception; (4) R-13 fixture (keys unattested) → S3 blocks, `blocked_reasons` rendered on the red status rail; (5) EFT mail fixture → attach + reconcile + SETTLED transition; ambiguous EFT (two claims, same reg) → review item; (6) grep-level assertion: no code path exists that initiates a funds transfer (CI test asserts the adapter op registry contains no payment-execution operation).