# Runbook — projection substrate and paste-assist mode (PRD-09 slice 1)

Owner: claims platform. Scope: `agents/projection_agent`, `packs/motor/projection`,
migration `0015_projections`, and the Claim-360 **Systems** tab.

PACKET-20 makes no external-system write. Paste-assist is authenticated human
work: the platform prepares an immutable snapshot and a copy strip, the officer
types into ICON or EDMS, and the officer attests. There is no adapter, no RPA
runner, and no reconciliation job in this slice — those are PACKET-21/22.

---

## 1. Blocked configuration ("nothing is executable")

**Symptom.** Claim 360 → Systems lists all fifteen operations but every row reads
`Pending capture` or `Blocked on inputs`, and a request returns
`blocked_on_inputs`.

**This is the correct production state.** No ICON or EDMS click path has been
captured (open item 3; `icon.reserve_adjust` is open item 17). PRD-11's
`icon.salvage_register` and PRD-12's three payment operations are registered so
the blocker is visible, and cannot be switched live by this packet.

**Do not** hand-edit `packs/motor/projection/operations.yaml` to `status: live`.
A live row must name a click path of the same operation and the same version, and
startup refuses anything else. Capturing a path is a discovery task (PRD-09 §9.4,
one day per system, recorded with the embed), not a config edit.

Check what the platform thinks:

```bash
python -c "from projection_agent.config import OperationRegistry; from pathlib import Path; r=OperationRegistry(Path('packs/motor/projection')); [print(row['id'], row['status'], row['blocked_on']) for row in r.catalogue()]"
```

---

## 2. Startup refuses to boot

`OperationConfigError` at `build_projection_agent` is always a configuration
defect, never a data problem. The message names the exact row or step. Common
causes:

| Message fragment | Cause |
| --- | --- |
| `operation catalogue is missing [...]` | a row was deleted; all fifteen ids are mandatory |
| `duplicate operation id` / `unknown operation id` | the catalogue no longer matches PRD-09 §9.2 |
| `is live without a click path` | `status: live` with `click_path_ref: null` |
| `version ... does not match the catalogue version` | the click path and catalogue drifted apart |
| `escapes the operation root` | a `click_path_ref` outside `packs/motor/projection` |
| `must declare an external_encoding` | a money or date binding with no captured target unit |
| `explicitly declared` | a generated value, value map, or undeclared literal |
| `outside the external-field dictionary` | a readback target that is not a registered `external.*` field |
| `undeclared validator` | `assert_format` names a validator the click path does not declare |

`LegacyProjectionCapabilityInUse` is different: it means an existing database
carries durable evidence (an agent run, promotion, grade, or event) for one of
the six provisional bare `icon.*`/`edms.*` capability ids PACKET-08 seeded.
Append-only history is never rewritten. Escalate to the repo owner for a
migration; do not delete the rows by hand.

---

## 3. Inspecting an exact snapshot

Snapshots are immutable and never mutated after creation.

```sql
SELECT id, operation, mode, status, idempotency_key, created_at, completed_at
FROM projections WHERE claim_id = :claim_id ORDER BY created_at DESC;
```

The payload carries `schema_version`, `operation_definition{operation, version}`,
the ordered `fields[]` (step id, canonical path, field id, field version, value
type, verification state, stored value, target encoding), the `source_event_id`,
and `snapshot_hash`.

`snapshot_hash` is SHA-256 over sorted, compact UTF-8 JSON of
`{operation_definition, fields}` with the hash member absent. The idempotency key
is `<claim_id>:<operation>:<snapshot_hash>`. Field version and
operation-definition version are **intentionally material**: a corrected input or
a re-captured click path produces a new projection rather than mutating one.

---

## 4. PII in a snapshot

A value whose field definition requires encryption is stored as the claim's
existing AES-256-GCM DEK envelope, exactly as `claim_fields` holds it. Vehicle
registrations keep the ED-6a plaintext exception.

* Plaintext appears **only** in the authenticated paste view.
* Every decrypt emits `pii.decrypted{field_path}` with the reading actor.
* Events, review payloads, logs, and ledger rows carry ids, paths, versions,
  hashes, and readback path *names* — never values.

To audit who read a claim's projection PII:

```sql
SELECT actor, payload, occurred_at FROM events
WHERE claim_id = :claim_id AND type = 'pii.decrypted' ORDER BY seq;
```

Deleting the claim DEK (seven-year crypto-shred) makes the snapshot values
unreadable. Projection rows are never purged independently of the claim.

---

## 5. Stale group updates and start/confirm idempotency

* **Start** requires `queued`. A repeat by the same or another authorised actor
  returns current state and **does not reset the clock**. Reads never start it.
* **Group update** requires `executing` and is reversible until confirmation. It
  cannot touch the payload. A `409 PROJECTION_STATE_STALE` means the projection
  moved on — reload the strip; the server is authoritative.
* **Confirm** requires every group done, literal `true` attestation, and exactly
  the declared readback keys.
  * same `Idempotency-Key` + same body → the first result;
  * same key + different body → `409 IDEMPOTENCY_CONFLICT`;
  * completed row + different readback → `409 PROJECTION_ALREADY_COMPLETED`.

`422 READBACK_FORMAT_PENDING_CAPTURE` means the captured validator for that path
is still `pending_capture`. That is a configuration gap, not an officer error: no
value is accepted until the format is captured.

---

## 6. Recovering a projection stuck in `verifying`

`verifying` means the attestation was accepted and the canonical readback field
write is in flight. Finalisation is crash-safe and idempotent:

1. the accepted attestation is persisted as `status=verifying`;
2. the canonical external field is appended through `ClaimService.write_fields`;
3. on resume the service recognises a current field whose `source_ref` names
   this projection and never appends a second version;
4. the row completes and emits exactly one `projection.completed`.

Resume every stranded row:

```bash
python -c "import app_wiring; app_wiring.app.state.projection_agent.resume(actor='system')"
```

`build_projection_agent` also resumes on boot, so a restart clears the backlog.
If a row will not leave `verifying`, check §7.

---

## 7. Conflicting readback

If a different current `external.icon.claim_no` is `human_verified`, belongs to
another projection, or holds a different value, finalisation stops in `verifying`
and creates `EXCEPTION{subtype: projection_readback_conflict}`. The platform
**never** supersedes the existing field and never picks a side — the target
system may legitimately have been edited out of band.

Resolution is a human decision through the review queue. Once the correct value
is established, the officer re-runs the operation from a fresh snapshot.

---

## 8. Cache dispatch and the `external_refs` mirror

`claims.external_refs` is written **only** by the existing `external_refs`
consumer, from the `field.updated` event. If the mirror looks stale, the outbox
has not been drained — run the dispatcher, do not update the column by hand:

```sql
SELECT consumer, status, attempts, last_error FROM event_deliveries
WHERE event_id IN (SELECT id FROM events WHERE claim_id = :claim_id) ORDER BY consumer;
```

---

## 9. FSM mismatch

A completed `icon.claim_register` owns exactly one hop:
`REPORT_RECEIVED → REGISTERED`, carrying the projection and readback refs.

* Already `REGISTERED` from this same projection → replay is a no-op.
* Any other state → **no** automatic transition, and one
  `EXCEPTION{subtype: projection_state_mismatch}` is created. The completed
  projection stays visible.
* Projection never owns or gates `REGISTERED → RESERVED`. That edge is C-02/C-03
  executed locally (PRD-08 §8.2); projection is a parallel tracker.

---

## 10. Weekly sampled readback review

A Celery Beat task (`projection_agent.sample_paste_readbacks`) runs on the
pack-configured schedule — Monday 08:00 EAT at launch, in
`packs/motor/projection/operations.yaml`. Changing the day, time, or rate is a
pack change, never a code change.

It scans completed paste-assist projections, skips any that already have a
`PASTE_READBACK_CHECK` source event, and selects the remainder with the existing
deterministic selector `sha256(projection_id) % 100 < rate_percent`. Selection is
therefore reproducible: the same projection is either always sampled or never
sampled at a given rate.

The review payload carries `projection_id`, `operation`, `capability_id`,
`snapshot_hash`, and `readback_paths` only. The workspace resolves detail from
the projection service, so no copied value is stored in the review.

Run it manually:

```bash
python -c "import app_wiring; print(app_wiring.app.state.projection_agent.sample_paste_readbacks())"
```

Repeat runs are idempotent. Mismatch comparison, `diverged` status, evidence
capture, and the no-auto-correction rule belong to PACKET-21.

---

## 11. What this slice deliberately does not do

No adapter registry entry, no `.execute`, no Playwright, no browser session, no
screenshot, no service account, no Secrets Manager client, no target network
call, no automatic reconciliation, no nightly drift job, and no adapter health.
The S-6 Admin adapter-health placeholder stays until PACKET-21; operation
availability is **not** adapter health and must not be reported as such.

No funds-transfer operation exists in the registry. PRD-12's payment operations
are blocked registry slots capped at L2 until that gate opens.
