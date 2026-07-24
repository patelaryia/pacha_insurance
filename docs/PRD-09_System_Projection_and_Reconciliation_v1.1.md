## PRD-09 — System Projection & Reconciliation (build spec)

> **v1.1, architecture freeze 2026-07-24** — paste-assist remains the safe
> production mode. The custom Playwright runner is superseded by ADR-003's
> purchased, vendor-neutral UI executor strategy. Where this document conflicts
> with Section 0 or Section 0.5, those files win. Anything underdetermined:
> follow ED-11.

### 9.1 Purpose

Move exact governed claim data into ICON and EDMS Powerhub without re-keying,
capture target-system readbacks, and guarantee zero silent divergence. Pacha
owns payload preparation, autonomy/authority, stable idempotency, evidence
requirements, target readback and reconciliation. A purchased UI executor may
perform clicks but does not own business decisions or truth.

### 9.2 Adapter and executor boundaries

```python
class Adapter(ABC):
    system: str  # 'icon'|'edms'|'finance'

    def health(self) -> AdapterHealth: ...
    def prepare(self, op: Operation, claim_id: str) -> PreparedOperation: ...
    def readback(self, op: Operation, keys: dict) -> dict: ...


class VendorUiExecutor(Protocol):
    vendor: str

    def execute(
        self,
        *,
        operation_ref: str,
        payload_ref: str,
        write_id: str,
        evidence_policy_ref: str,
    ) -> ExecutorReceipt: ...
```

`Adapter.prepare` resolves only registered, sufficiently verified Pacha fields
and returns an immutable typed snapshot. The `VendorUiExecutor` receives the
minimum accepted execution contract and may perform declared UI actions. It
cannot choose values, broaden authority, mutate the payload, retry an uncertain
write, mark reconciliation complete or write Pacha domain tables directly.
Every executor invocation is the deferred action behind `execute_or_stage`;
there is no second execution path.

Registered operations v1 remain:
`icon.policy_read`, `icon.claim_register`, `icon.reserve_create`,
`icon.reserve_breakdown`, `icon.reserve_adjust` (still `pending_capture`),
`icon.assessor_payment_request` (not behind GP-1, max L3 with permanent
sampling), `icon.note_entry`, `icon.claim_details_report`,
`icon.salvage_register`, `icon.payment_voucher`, `edms.general_payments`,
`edms.claims_workflow`, `edms.attach_and_tag`, `edms.claim_payment`,
`edms.payment_workflow`.

Config maps each operation to
`mode ∈ {paste_assist, vendor_executor, api}`. `paste_assist` is always a legal
fail-closed fallback. `vendor_executor` stays `blocked_on_inputs` until one
vendor and operation passes ADR-003's security, DPIA, procurement, service
identity, reachability, evidence and staging-acceptance gates. Evaluate
Microsoft Power Automate Desktop first; evaluate UiPath only if Power Automate
cannot meet them. No vendor is selected by this PRD.

```sql
CREATE TABLE projections (
  id TEXT PRIMARY KEY, claim_id TEXT NOT NULL, operation TEXT NOT NULL,
  mode TEXT NOT NULL, status TEXT NOT NULL,   -- 'queued'|'executing'|'verifying'|
                                              -- 'completed'|'failed'|'diverged'
  payload JSONB NOT NULL,                     -- encrypted typed field/version snapshot
  readback JSONB, divergence JSONB,
  evidence JSONB,                             -- immutable executor refs or paste attestation
  attempts INT DEFAULT 0, idempotency_key TEXT UNIQUE,
  created_at TIMESTAMPTZ, completed_at TIMESTAMPTZ
);
```

Idempotency key = `(claim_id, operation, payload_hash)`. The same key is the
executor `write_id`. Duplicate triggers return the existing projection. Before
any retry after a lost/failed receipt, Pacha must independently prove through a
target-specific read/probe that no write occurred. If it cannot, create
`EXCEPTION{type: uncertain_write}` — **never blind-retry a write**.

Claim PII in `projections.payload` remains protected by the claim DEK per
ED-6a. Events, Temporal histories, executor logs and ordinary APIs carry only
opaque ids, paths, versions and hashes. Vendor access to any target-visible
personal data requires a completed data-processing review and DPIA.

### 9.3 Mode 1 — Paste-assist (ships first and remains supported)

The Claim 360 Systems tab shows one field strip per pending operation, ordered
to the captured target form. Each typed field has a copy button, grouped by
screen, with "done" checks. Readback capture is inline. After
`icon.claim_register`, the officer records the ICON claim number; Pacha writes
canonical `external.icon.claim_no` with `projection_readback` provenance and
updates the denormalised cache through its dedicated consumer.

Completion requires an officer attestation that values were entered as shown.
Weekly deterministic 10% `PASTE_READBACK_CHECK` sampling independently checks
the target. Paste duration is recorded per operation. Paste-assist never claims
automated readback or exactly-once execution.

### 9.4 Mode 2 — Purchased UI executor

Pacha does not build a Playwright runtime, browser hosting, runner leases,
session management or generic selector framework.

Each accepted operation has a versioned vendor-neutral definition containing:

- exact typed inputs and target encodings;
- preconditions and target release/version;
- stable Pacha `write_id`;
- declared UI steps or vendor flow reference;
- postcondition and non-execution probe;
- evidence requirements and immutable evidence references;
- readback keys and validators;
- per-step timeout and explicit known-failure handling; and
- `failure_policy: fail_closed`.

Vendor AI selector repair/self-healing is disabled contractually and
technically where the product allows. Selector miss, multiple matches,
unexpected screen or target-release mismatch immediately stops execution,
creates `EXCEPTION{type: ui_drift}`, records available evidence, opens the
operation circuit and falls back to paste-assist. The executor never hunts for
an alternative element.

Dedicated service identities have least privilege and no approval rights.
Credentials live in the vendor's approved managed credential store or AWS
Secrets Manager as selected by the security design; never in Pacha source,
payloads, Temporal history or evidence. Production activation requires
operation-specific staging evidence. Synthetic tests establish only the
contract mechanics.

### 9.5 Reconciliation (non-negotiable invariant)

Every apparently completed external write moves to `verifying`. Pacha then
independently reads the record from the target through `Adapter.readback`.
Executor success is evidence, not readback and not completion.

Compare the target readback against the immutable payload snapshot. Mismatch
sets `status='diverged'`, emits `projection.diverged`, and creates
`EXCEPTION{type: divergence}` showing both values and evidence references. Pacha
never auto-corrects a divergence. A human decides which system is wrong.

Paste-assist uses officer attestation plus the weekly sample. When automated
target read exists, a standing Pacha job re-reads claims' key financial fields
and status; any drift pages the team. Pacha owns this job even when a vendor
performs UI execution. Dashboard divergence target remains zero.

### 9.6 Capabilities and activation

Capabilities remain `project.<operation>`. Paste-assist is L1 by construction.
An accepted vendor-executor operation starts at L2 (officer-watched), then
follows PRD-03 promotion with 20% sampling at L3. Money-adjacent ceilings and
GP-1 remain unchanged. `icon.payment_voucher`, `edms.claim_payment` and
`edms.payment_workflow` cannot exceed L2 before GP-1 and never exceed L3 after
it.

Activation order remains `edms.claims_workflow` first, then
`icon.claim_register` + reserve operations, then `edms.attach_and_tag`.
`PACKET-21` and `PACKET-22` are frozen; neither is complete. Reissue them as
vendor-evaluation/control-contract and operation-activation packets after the
RFI, without reviving the custom runner.

### 9.7 Acceptance

1. Paste-assist registration uses the exact captured order, writes canonical
   readback provenance and records one consolidated entry session.
2. Duplicate projection triggers and duplicate executor starts with one
   idempotency key produce one projection and at most one proven target write.
3. A selector miss or self-healing attempt fails closed as `ui_drift`, opens the
   operation circuit and falls back to paste-assist.
4. Killing/loss of the executor after submit but before receipt produces
   `uncertain_write` unless Pacha's independent probe proves non-execution; no
   automatic retry occurs.
5. Deliberate target drift is found by Pacha reconciliation and becomes
   `diverged`.
6. Contract tests prove no executor can bypass `execute_or_stage`.
7. Logs, events, Temporal history and executor receipts contain no copied claim
   PII or credentials.
8. One Power Automate operation is trialled first in an approved staging target;
   UiPath is evaluated only if the recorded Power Automate control assessment
   fails. No synthetic result is described as live acceptance.
