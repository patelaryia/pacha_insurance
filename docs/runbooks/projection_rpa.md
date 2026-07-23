# Runbook — PRD-09 projection RPA control plane

**Scope:** the machinery PACKET-21 ships. **Nothing in production is live.** Every
ICON and EDMS operation is `pending_capture`/`blocked_on_inputs`, no target
endpoint or service account is installed, and the internal runner routes are not
mounted. If any procedure below appears to apply to a real Mayfair system today,
the configuration is wrong — stop and escalate.

---

## 0. Is the control plane live?

```
GET /console/ops/packs            # admin or auditor
```

`adapter_health` returns one row per system:

| Field | Meaning |
| --- | --- |
| `status` | `healthy` \| `degraded` \| `unavailable` \| `circuit_open` |
| `reason_code` | `pending_capture`, `no_runner_heartbeat`, `ui_drift`, … |
| `runner_last_seen_at` | last outbound runner heartbeat, or `null` |
| `circuit_operation_ids` | operations with an open breaker |

Today both rows read `unavailable / pending_capture`. The application also
reports `blocked_on_inputs: runner-machine-identity` for the runner control API,
and the runner container exits `78` with its blocker list.

---

## 1. Gate staging (an L2 launch)

A `mode=rpa, status=live` projection is authorised through AR-2 before anything
leaves the platform.

* **L2** creates one `DRAFT_RELEASE{projection_rpa}` naming the exact projection
  id, definition version, and snapshot hash. No lease exists yet, and no browser
  has opened.
* **Approve** launches exactly that projection. **Edit→Approve** changes only
  that row to paste assist. **Reject** launches nothing and leaves the row
  available in paste assist.
* Any changed id, hash, version, or operation is `409`. Replaying the exact
  approval creates one leaseable job, never two.
* **L3/L4** authorise a deferred job immediately; L3 keeps the existing 20%
  `SAMPLE_REVIEW`.

Check: `GET /console/claims/{claim_id}/projections/{projection_id}/rpa` →
`gate.state` and `gate.review_id`.

---

## 2. Inspecting a lease

The RPA panel shows `attempt`, `lease.runner_id`, `lease.expires_at`, and the
current step. Durable state lives in `projections.evidence.rpa`:

```
evidence.rpa.lease         # token_sha256, runner_id, attempt, run_id, expiry
evidence.rpa.attempts[]    # per attempt: outcome, write_ids, frames
evidence.rpa.authorisation # the gate receipt
```

The **raw lease token is never stored** — only its SHA-256. A callback with a
wrong token, a wrong runner, a stale attempt, or an expired lease returns
`404`/`409` and mutates nothing.

---

## 3. Safe reaping

`projection_agent.reap_leases` runs every `reaper_seconds` (60) and handles a
stale `executing` row. It **never** calls `Adapter.execute`.

1. read the durable last-completed step, effect class, write ids, and evidence;
2. call the captured prior-completion probe through `Adapter.readback`;
3. one exact matching target record → `verifying`, then finish with **no**
   second write;
4. proven absence **and** no `external_write` completed → back to authorised
   `queued`;
5. ambiguous, unavailable, or any possible write without exact proof →
   `failed` + `EXCEPTION{uncertain_write}`;
6. after three safe attempts → `failed` + `EXCEPTION{projection_attempts_exhausted}`.

To force a cycle: `celery -A claim_core.celery_app call projection_agent.reap_leases`.

---

## 4. Uncertain writes

`EXCEPTION{uncertain_write}` means **a person must establish target state first.**

The review carries projection, operation, run, and attempt ids, the write ids,
the last step, and evidence ids. It carries no guessed completion, no value, no
credential, no selector, and **no retry button**. The row is terminal and is
deliberately *not* offered as paste assist.

Resolution path: read the evidence frames, look at the target, then either
create a new projection under ordinary workflows (if nothing landed) or record
the outcome and move on (if it did). Never re-lease the old row.

---

## 5. Circuit breaker: open and reset

A selector that resolves to zero or several elements, or a failed captured
pre/postcondition, is `ui_drift`. The response is always: halt, capture the
failure frame, one `EXCEPTION{ui_drift}`, and a durable breaker in
`platform_state` under `projection.circuit.<operation>`.

* If evidence proves **no** write completed, the same row becomes
  `mode=paste_assist, status=queued` and the attempt stays in evidence.
* If a write may have occurred, the row is `failed` with `uncertain_write`.

The signed pack file is never edited at runtime. Clearing requires an
admin/claims-manager and **all** of:

1. a strictly newer operation-definition version installed;
2. its synthetic selector/reconciliation suite passing;
3. adapter health `healthy`.

```
POST /console/ops/projection-circuits/{operation_id}/clear
```

Refusals return `409 PROJECTION_CIRCUIT_BLOCKED`. Clearing is ledgered and
affects **new** projections only; no terminal row is ever reused.

---

## 6. Evidence access

Every executed step has one `before` and one `after` frame; a failure captures a
third when the browser can still take one. Credentials and login fields are
never photographed.

```
GET /console/claims/{claim_id}/projections/{projection_id}/evidence/{evidence_id}
```

Same-claim RBAC applies, the response is `private, no-store`, the digest is
verified against the manifest, and the read appends `pii.decrypted` with
`resource_type=projection_evidence`. The API never returns a blob key.

Target screens may contain claim PII. These frames are claim evidence: SSE-KMS
in production, Glacier Instant Retrieval at 90 days, deleted/crypto-shredded
with the claim at seven years (ED-9).

---

## 7. Reconciliation

Every successful write enters `verifying`. It can never complete directly.

The platform validates readback keys against the exact operation version,
applies **only** the declared typed normalisers, decrypts expected values
through claim core, compares every declared input and output to the immutable
snapshot, verifies evidence completeness, and records one critical projection
`G-VAL`. It completes only on an exact full match.

There is no implicit trimming, case folding, locale parsing, thousands
separator, currency prefix, timezone conversion, or rounding. A target
representation nobody captured **diverges** — it is never coerced.

---

## 8. Divergence

On mismatch: expected and actual are protected with the claim DEK in
`divergence`, the row becomes `diverged`, one `projection.diverged` is emitted
and ledgered, one `EXCEPTION{divergence}` is created, a critical G-VAL failure
is recorded (which lets the existing autonomy rules demote an L3+ capability),
and the assigned officer is notified through the existing rule.

Nothing is auto-corrected. Resolution records exactly one disposition:

| Disposition | Meaning |
| --- | --- |
| `target_out_of_band` | someone changed the target outside the platform |
| `platform_snapshot_wrong` | the platform value was wrong → correction corpus |
| `target_readback_wrong` | the readback misread the target |
| `unresolved` | not established |

A corrected claim value or a target retry requires a **new version / new
projection** under ordinary workflows.

---

## 9. Paste sampling

The weekly sample creates `PASTE_READBACK_CHECK`. Resolution is two-step:

```
POST /console/reviews/{review_id}/paste-readback/capture
```

The officer types exactly the declared target values (and may attach a
screenshot). The service protects them under the claim DEK, stores them under a
random `capture_id`, and returns only the capture id, mismatch paths, and
hashes. Observed values never enter the review item, the event, or the log.

* **Approve** only when the server comparison is exact.
* **Edit→Approve** only when the declared mismatch paths equal the server's;
  it transitions `completed → diverged`.
* **Reject** requires a reason and creates
  `EXCEPTION{paste_readback_unavailable}` — it never invents a match.

---

## 10. Standing drift

`projection_agent.nightly_drift` is registered but **not scheduled**: the
nightly EAT time, the ICON claim-status map, and the target selectors are not in
the source documents (`packs/motor/projection/drift.yaml` is `pending_capture`,
register #290). Running it today returns `blocked_on_inputs`.

When PACKET-22 captures them, drift reuses the same normalisers and the same
divergence lifecycle. It reads only claims with a canonical external reference,
a prior completed RPA projection at the same definition version, a live mapping,
and no unresolved divergence for that projection/path. An exact repeat creates
no duplicate event or review.

---

## 11. Health and total runner shutdown

Health is derived from captured availability, the runner heartbeat, the adapter
probe, and circuit state. To stop all robotic execution immediately, in
increasing order of bluntness:

1. **stop the runners** — no runner heartbeat degrades the adapter within one
   heartbeat interval, and a degraded adapter is never granted a lease;
2. **open the breakers** — no new lease for those operations; in-flight rows
   still reconcile;
3. **demote the capability below L2** — a live RPA definition below L2 is
   invalid and refuses at startup.

None of these touches an in-flight target write. A row already `executing` is
resolved by §3, never by force.

---

## 12. What this packet does not discharge

Service-account creation (item 1), DG-1/browser accessibility and an ICON test
instance (item 2), ICON/EDMS path, value-map, and failure-signature capture
(items 3/17), runner host selection (item 12), the production runner machine
identity, S3/KMS/Object-Lock certification (item 30), and every live replay,
selector-injection, duplicate-filename, crash, or drift test. Those are
PACKET-22, infra, and live-gate work.
