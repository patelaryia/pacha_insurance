# Assessment dispatch runbook

## What is live

PACKET-16 owns the assessment front half: a verified repair estimate advances an eligible
claim to `IN_ASSESSMENT`, opens an undetermined `MODE_CONFIRM` card, logs the permanent-L0
shadow comparison, and stages one T-11 dispatch per officer-selected assessor. T-11 and the
assessor reminder template remain `pending_capture`; a staged draft is the expected launch
outcome and no `email.sent` event should exist.

## Mode-card triage

The officer must choose `desk` or `physical` and one or more active assessor vendors. R-06
is intentionally `blocked_on_inputs`, so the card's `undetermined` result is not an error and
the officer choice is labelled training data. Unknown or inactive vendors return
`422 VENDOR_NOT_REGISTERED` and leave the card open. A reject creates a new card linked by
`retry_of`; repeated estimate events while a card is open do nothing.

If `EXCEPTION{assessment_out_of_sequence}` appears, inspect the claim timeline. Only
`TRIAGED` and `AWAITING_DOCS` are valid pre-assessment starting states; repair the lifecycle
state rather than forcing dispatch.

## Dispatch and missing inputs

Each selected firm should have one claim-scoped `assessor` party, one open T-11
`DRAFT_RELEASE`, one `assessment.dispatched` event, and one `assessor_report` chase
checklist. The broker is represented as a second recipient because the current transport has
no cc channel. Garage details and unavailable claim-form/logbook attachments are listed under
`missing` and `missing_attachments`; do not fill those values from inference.

## SLA and reminders

`assessment.dispatched` starts a separate `assessor_turnaround` business-day clock keyed by
`assessor_party_id`. At T+3 business days the existing chase tick stages T-06r-assessor to
that assessor only. PACKET-17 will emit `assessment.report_received` to stop the matching
clock. Breach routing remains visible but `escalate_to_role` is still `pending_capture`.

Useful checks:

```sql
SELECT definition_id, state, warn_at, breach_at
FROM sla_clocks WHERE claim_id = :claim_id;

SELECT purpose, requester_party_id, status
FROM chase_checklists WHERE claim_id = :claim_id;
```

## Report attribution and verification

PACKET-17 attributes an `assessor_report` only when the inbound communication sender
matches exactly one dispatched assessor party email. `EXCEPTION{report_unattributed}` means
the sender did not resolve uniquely; identify the firm and reprocess rather than attributing
by elimination. A successful match emits `assessment.report_received` and stops only that
party's turnaround clock.

The cascade requires `assessment.agreed_quote`, and `assessment.pav` when supplied, at
confidence 0.90 or as `human_verified`. Below-floor or unresolved-citation values wait on
their `FIELD_VERIFY` cards; the cascade never repairs or guesses document provenance.

## Fees, reserves, and projection

Assessor fees are visible in the persisted extraction but are not canonical extraction
targets. C-02 therefore records `blocked_on_inputs` until an officer keys both
`assessment.assessor_fee` and `assessment.reinspection_fee` from the cited report. The
subsequent field events automatically re-attempt C-02, append `reserve.total` with calc-run
provenance, and emit `projection.requested`. Never substitute the vendor registry's standard
fee.

Useful checks:

```sql
SELECT status, missing_inputs FROM calc_runs
WHERE claim_id = :claim_id AND calc_id = 'C-02' ORDER BY ts, id;

SELECT path, value, source_ref FROM claim_fields
WHERE claim_id = :claim_id AND path = 'reserve.total'
ORDER BY version DESC;
```

## Multi-assessor selection

Multi-assessor claims wait for every dispatched firm. When an outstanding firm's
`assessor_turnaround` clock breaches, `PROCEED_PARTIAL` lists received and outstanding party
ids. Approve only when the received evidence is sufficient; rejection leaves selection
pending. Selection is deterministic: lowest verified agreed quote, then lowest party id on a
tie. The comparison is durable in `assessment.selection_completed`. A later differing human
quote remains in force and raises `EXCEPTION{selection_overridden}` for documentation.

## Savings audit

`savings_ledger` is append-only. `assessment_negotiation` rows are the billable header delta;
`supplier_substitution` rows are evidence only. The MTD/YTD tile sums header rows exclusively.
Every row must retain document/calc evidence. Investigate missing citations before treating a
row as contract-billable, and never reconcile supplier lines arithmetically to the header.

```sql
SELECT kind, baseline_amount, achieved_amount, saving, evidence
FROM savings_ledger WHERE claim_id = :claim_id ORDER BY occurred_at, id;
```
