# Document-chase agent runbook

The PRD-06 chase agent is installed after the intake agent with
`build_chase_agent(app)`. It consumes `chase.init`, claim-state changes, and
document classification/extraction events. The 15-minute scheduler calls
`app.state.chase_agent.tick()`.

## Reminder operations

Cadence, deferral, cap, and recipient-ladder values are owned by
`packs/motor/chase/chase.yaml`. Requests and reminders are currently visible
`DRAFT_RELEASE` work because the T-06/T-06r bodies and Graph transport are
pending capture. Do not treat a staged draft as `email.sent`.

An initial T-06 attempt outside the AR-3a window remains durable as unrequested
pending items. Each 15-minute tick retries those checklists, so the first
in-window tick stages the request and starts item clocks. A refused outcome
remains retryable and must also leave its gate/review evidence visible.

`EXCEPTION{chase_exhausted}` means at least one outstanding item reached six
reminders. Confirm the requester and document need, then resolve the work item;
the engine will not create a seventh reminder. Terminal claims cancel their
open checklists and must produce no further attempts.

At L3+, the pending Graph transport can refuse the same due reminder on every
tick, producing repeated blocked action-run rows. Until item 1 installs the
transport/release queue, treat these as one dependency outage; do not close the
underlying checklist or advance its cadence.

## Physical receipt and waivers

Use `POST /chase/items/{id}/attest` only after physically sighting the original
logbook or keys. The action records the actor and appends the corresponding
human-verified `salvage.logbook_held` or `salvage.keys_held` field version.

Blocking surrender-item waivers require a `claims_manager` and a non-blank
reason. Review `chase.item_received` and `chase.item_waived` in the audit ledger
when investigating a settlement-gate decision. R-14 deliberately remains
blocked until PRD-12 registers its payee input.

If `kra_pin` and surrender `kra_pin_cert` are both outstanding, an inbound KRA
certificate deliberately raises `chase_match_ambiguous`. Do not choose one.
Resolve one requirement independently (or have a claims manager waive a
justified blocking item); a later distinct certificate event can then match the
sole remaining item. Manual document reuse/replay remains pending capture.

## Diagnostics

- `GET /chase/claims/{claim_id}` shows every never-purged checklist item.
- `GET /portfolio` exposes document-cycle, broker-response, and claim chase-time
  series; chase-cycle rows are keyed by checklist id and carry claim id, and each
  series also has a CSV endpoint.
- If reminders are unexpectedly absent, check claim suppression state,
  `snooze_until`, a reply within the deferral window, template status, and the
  AR-3 send window in that order.
