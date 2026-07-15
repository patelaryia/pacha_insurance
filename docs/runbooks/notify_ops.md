# Notify and operations surfaces runbook

## Purpose

PACKET-12 installs the AR-5 staff notification projection and the PRD-04
approval, portfolio, SLA, and administration surfaces. Staff notifications are
not claim-party communications: they bypass G-COMM and the autonomy gate, but
every `sent`, `staged`, and SLA escalation fact still enters the single-writer
audit ledger through the event dispatcher.

## Staged email backlog

Email transport is deliberately `pending_capture` until open item 1 supplies
the shared mailbox and service accounts. Inspect
`GET /console/ops/notifications?scope=mine` or query `notifications` for
`channel='email' AND status='staged'`. Each row must carry
`payload.blocked_on = open-item-1`. Do not mark these rows sent, replay them to a
mock transport, or delete them. When credentials land, change the pack transport
configuration and drain staged rows through the future reviewed Graph transport.
That transport packet must enforce `staff_domain_allowlist` before enabling any
Graph send; PACKET-12 validates the allowlist but deliberately has no live email
transport on which to enforce it.

## Websocket connection failures

The browser sends `{"token":"…"}` as its first websocket message to
`/console/ops/ws`. Invalid, expired, unmapped, or unverifiable identities close
with code 4401. Confirm that the same Entra verifier and immutable `(tid, oid)`
mapping used by HTTP console ingress are installed. Durable in-app rows remain
visible through the notifications GET route even when live push is unavailable.

## Digest reruns

The Beat entry `notify.daily_digest` runs at 08:00 Africa/Nairobi from
`packs/motor/notify/notify.yaml`. A manual rerun calls
`app.state.notify.run_digest(now)` with an aware timestamp. The projection key is
unique per officer, channel, and EAT date, so same-day reruns create no duplicate
rows. Digest values are derived only from committed claims, review items, and SLA
clocks.

## Escalation blocked rows

The SLA board sorts every open clock by `breach_at`, nulls last. Bulk escalation
returns one result per requested clock. `blocked_on_inputs` means the persisted
SLA definition still has `escalate_to_role: pending_capture`, the target role has
no configured recipient, or the clock is no longer open. Do not select a role
locally. A successful escalation emits `sla.escalated`; it does not change claim
or clock state. Repeating the same bulk request emits another escalation and is
therefore noisy by design under register #112; no deduplication or hidden clock
mutation is implied.

## Dashboard blocked series

Live tiles are point-in-time or EAT MTD/YTD calculations and expose CSV exports.
The autonomy, no-touch, and median-handling trends remain `pending_capture` under
register #79. Their CSV routes correctly return
`409 SERIES_BLOCKED_ON_INPUTS` until the CM-approved window and denominator
definitions are committed in `packs/motor/dashboard.yaml`.
