# Runbook — claim_core (PRD-00)

## Services

| What | Where | Cadence |
|---|---|---|
| Outbox dispatch | Celery task `claim_core.dispatch_events` (all consumers except ledger) | every 2s |
| Ledger consumer | Celery task `claim_core.dispatch_ledger`, queue `ledger`, **concurrency=1** | every 2s |
| SLA evaluation | `claim_core.evaluate_slas` (Beat) | every 5 min |
| Ledger verification | `claim_core.verify_ledger` (Beat) | nightly 00:30 EAT |

All four are one-line wrappers; the logic is synchronous-drivable (`dispatch_once()`,
`evaluate()`, `run_nightly_verification()`) for local debugging.

## Dispatcher recovery

- Worker death mid-consume is safe: the attempt is counted **before** the consumer
  runs; the delivery stays `pending`/`failed` and retries on the backoff schedule.
- Delivery states: `pending → succeeded | failed (retry) | dead_letter` (after 8
  attempts). Dead-letters emit an `ops.alert` event carrying `failed_consumer`.
- To replay a dead-letter after fixing the consumer: set its `event_deliveries` row
  back to `failed`, `attempts = 0`; next poll picks it up. Never delete delivery rows.
- Stuck queue check: `SELECT consumer, status, COUNT(*) FROM event_deliveries GROUP BY 1, 2;`

## Audit-degraded mode (§0.6)

Nightly verification failure sets `platform_state`: `audit_degraded = true`,
`autonomy_promotions_frozen = true`, and emits `ops.alert{audit_chain_verification_failed}`
with `first_bad_seq`.

Procedure:
1. Page acknowledged → freeze stays until incident review completes (mandatory).
2. Inspect the row at `first_bad_seq`; compare against the S3/WORM anchor for the
   preceding day (`audit-anchors/<date>.json`) and the event it derived from
   (`detail.event_id`).
3. Chain repair is a manual, incident-reviewed operation — there is no automated
   rewrite path by design.
4. After repair: `run_nightly_verification()` must return `ok`, then clear the two
   `platform_state` flags in the same change window. **No automatic demotions** —
   agents are unaffected throughout.

## PII / keys

- Local/dev: `PACHA_LOCAL_MASTER_KEY` + `PACHA_LOCAL_INDEX_KEY` (base64, 32 bytes).
  Unset → ephemeral keys: fine for tests, **data unreadable after restart** — never
  run dev with real data (prod PII never leaves prod, ED-3).
- Prod: KMS provider lands with the infra packet (register #30); until then prod
  launch is blocked.
- Key rotation (local): decrypt-all/re-encrypt is not implemented; rotation before
  the KMS provider means re-ingesting. Plates are plaintext by design (ED-6a).
- Every decrypt writes a `pii.decrypt` ledger row — sudden volume spikes in
  `SELECT COUNT(*) FROM audit_ledger WHERE action='pii.decrypt'` are an access-audit
  signal, check actor distribution.

## SLA clocks

- `approval_dwell` is `blocked_on_inputs` (register #28) — the engine refuses to
  start it; unblocking requires captured business-hours bounds + register update.
- Clocks are never purged (ED-9). "Open" = `stopped_at IS NULL`.
- Movable holidays are not in `holidays.yaml` (register #29) — business-day maths
  are wrong on those days until captured; affects `assessor_turnaround` only.
