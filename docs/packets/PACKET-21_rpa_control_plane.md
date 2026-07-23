# PACKET-21 — RPA runtime and zero-silent-divergence control plane (PRD-09 slice 2 of 3)

> **Status:** issued · **Builder:** Codex per `AGENTS.md` · **Reviewer:** CTO per
> `CLAUDE.md`
>
> **Source spec:** `docs/PRD-09_System_Projection_and_Reconciliation_v1.1.md`
> §9.2 and §9.4–§9.6, with synthetic coverage of acceptance scenarios 2–6;
> PRD-03 §3.3–§3.5; PRD-04 §4.2–§4.5; PRD-08 §8.3; Section 0.5
> AR-1/AR-1a/AR-2; Section 0 ED-1/ED-3/ED-3a/ED-6/ED-6a/ED-7/ED-7a/
> ED-8/ED-9/ED-10/ED-11; guide §3–§6; PACKET-20 unchanged hand-off;
> registers #1–#3/#12/#17/#30/#68/#71/#117/#224/#252/#261–#275 and
> #276–#296 below.
>
> **Depends on:** PACKET-20 merged and green.
>
> **Acceptance:** new protected backend, console, runner-contract, and
> PostgreSQL Packet-21 suites. The builder does not weaken Packet-01–20
> acceptance.
>
> **Next packet:** PACKET-22 — captured ICON/EDMS operation definitions,
> production triggers, service identities, controlled activation, and live
> PRD-09 acceptance.

## 0. CTO disposition and slice boundary

PRD-09 remains three packets:

1. **PACKET-20:** durable projection substrate + permanent paste-assist mode;
2. **PACKET-21:** adapter/RPA runtime + zero-silent-divergence control plane;
3. **PACKET-22:** captured production operation activation + live acceptance.

PACKET-21 builds the machinery that makes an external write governable and
recoverable without pretending that an ICON or Powerhub path has been
captured. It owns:

- the binding PRD-09 `Adapter`, `AdapterHealth`, and `OpResult` contracts;
- one deferred-action extension to the existing AR-2 gate;
- an outbound-only runner container and authenticated queue protocol;
- exact click-path execution with isolated Playwright sessions;
- durable leases, heartbeats, attempts, and crash recovery using existing
  `projections` and `agent_runs` storage;
- screenshot evidence before and after every executed step;
- explicit irreversible-write boundaries and prior-completion probes;
- automatic readback after every completed write;
- typed reconciliation against the immutable projection snapshot;
- `failed` and `diverged` projection lifecycles;
- selector-drift circuit breaking and safe paste-assist fallback;
- the two PRD-09 EDMS known-failure handlers;
- secure resolution of `PASTE_READBACK_CHECK`;
- the standing drift engine and a fixture schedule;
- adapter health in S-6, RPA progress in Systems, and the divergence-rate tile;
- notification, ledger, grader, and runbook completion for divergence.

It deliberately does **not**:

- add or activate a production ICON/EDMS click path;
- change any production operation in `operations.yaml` from
  `pending_capture`/`blocked_on_inputs`;
- install a target-system credential or a production runner machine identity;
- invent an ICON claim-number format, dropdown map, selector, target
  normalisation, retry key, reserve/status readback map, or nightly time;
- add automatic operation triggers beyond PACKET-20's existing reserve event;
- execute any PRD-11/PRD-12-owned operation;
- make approval-pack manifest items 12/13 projection-readable;
- implement `api` mode, a funds transfer, or a payment release;
- claim a staging/training or live target-system acceptance run.

All external-system acceptance in this packet uses a deterministic synthetic
target served inside the test process. Fixture definitions live under tests,
not the production motor pack. A green Packet-21 build proves the runtime and
control invariants; it does not prove that Mayfair's systems are reachable or
that a production selector is correct.

## 1. Deliverables and package boundary

Extend the existing PRD-09 package; do not create a second projection package:

```text
agents/projection_agent/
  adapters.py          # Adapter/health/result contracts only
  rpa.py               # governed coordinator + deferred gate executor
  runner_api.py        # authenticated pull/heartbeat/evidence/result API
  reconcile.py         # canonical compare + divergence lifecycle
  drift.py             # standing read-only reconciliation task
  service.py           # existing facade, extended without breaking PACKET-20
  config.py            # full executable click-path + runtime validation
  tasks.py             # lease reaper + drift Beat registration
  api.py               # Systems/readback/evidence routes
  runner/
    __init__.py
    client.py           # outbound queue client; no server
    browser.py          # exact Playwright execution
    target_adapters.py  # ICON/EDMS adapter slots; production-disabled

infra/rpa_runner/
  Dockerfile
  entrypoint.py
  targets.yaml         # environment endpoint/secret-reference slots, blocked
  README.md

packs/motor/projection/
  operations.yaml      # remains production-blocked
  runtime.yaml         # runner/reconciliation control values
  drift.yaml           # production drift registry remains pending_capture

packs/motor/review/
  contracts.yaml
  schemas/DRAFT_RELEASE_PROJECTION@1.json
  schemas/PASTE_READBACK_CHECK@2.json
  schemas/EXCEPTION_DIVERGENCE@1.json

tests/acceptance/test_packet_21_rpa_control_plane.py
tests/acceptance/console/test_packet_21_console.test.tsx
tests/acceptance/test_packet_21_runner_contract.py
tests/fixtures/projection/   # complete synthetic paths + target only

docs/runbooks/projection_rpa.md
```

No Alembic migration ships. PACKET-21 reuses, without changing:

- the exact `projections` DDL from PRD-09/0015;
- `agent_runs` from AR-1;
- `platform_state` as the existing small durable control-state registry;
- the claim-core blob-store boundary;
- the closed 17-type review enum;
- the existing event catalogue.

No package imports another package's private model. Projection continues to use
the public claim-core, review-queue, eval-harness, agent-runtime, notify, and
blob-store facades. The only cross-package runtime change is the explicitly
curated deferred-executor contract in `agent_runtime`.

## 2. Unchanged projection storage and extended lifecycle

The PRD-09 table remains exact. Do not add a lease, runner, job, health,
evidence-artifact, error, or reconciliation column.

PACKET-21 may mutate only the columns already authorised by PACKET-20:

```text
mode
status
readback
divergence
evidence
attempts
completed_at
```

`payload`, `claim_id`, `operation`, `idempotency_key`, and `created_at` remain
immutable.

Add these legal state edges:

```text
queued     -> executing    authorised runner claims a lease
executing  -> verifying    all declared writes returned and readback begins
executing  -> queued       safe pre-write RPA failure falls back on the same row
executing  -> failed       known terminal failure or uncertain write
verifying  -> completed    every declared value reconciles exactly
verifying  -> diverged     immediate RPA readback mismatch
completed  -> diverged     sampled paste mismatch or later standing drift
```

Binding rules:

- every transition takes the existing projection lock;
- a terminal `failed|diverged` row never returns to execution;
- `completed_at` is set only for `completed|failed|diverged`;
- `attempts` increments once, atomically, when a runner lease is granted;
- an unclaimed queued row remains attempt 0;
- a safe pre-write fallback retains its attempt evidence and returns to
  `queued` with `mode=paste_assist`;
- a runtime circuit breaker affects the current row only through that explicit
  fallback edge; all other configuration changes affect new rows only;
- an exact repeated callback returns the stored outcome;
- a stale lease/result from another attempt returns 409 and mutates nothing.

PACKET-20 paste-assist edges and responses remain unchanged.

## 3. Public adapter and result contracts

Add the PRD-09 interface at the public projection boundary:

```python
class Adapter(Protocol):
    system: Literal["icon", "edms", "finance"]

    def health(self) -> AdapterHealth: ...

    def execute(
        self,
        op: Operation,
        payload: dict[str, object],
        run_id: str,
    ) -> OpResult: ...

    def readback(
        self,
        op: Operation,
        keys: dict[str, object],
    ) -> dict[str, object]: ...
```

Closed health result:

```python
@dataclass(frozen=True)
class AdapterHealth:
    status: Literal["healthy", "degraded", "unavailable", "circuit_open"]
    checked_at: datetime
    system: Literal["icon", "edms", "finance"]
    runner_id: str | None
    reason_code: str | None
```

Closed execution result:

```python
@dataclass(frozen=True)
class OpResult:
    outcome: Literal[
        "submitted",
        "completed_existing",
        "ui_drift",
        "known_failure",
        "uncertain_write",
    ]
    last_completed_step: str | None
    write_ids: tuple[str, ...]
    readback_keys: dict[str, object]
    evidence_ids: tuple[str, ...]
    reason_code: str | None
```

Free-text target errors, stack traces, HTML, screenshots, credentials, selector
contents, or values are never carried in `OpResult`. The runner maps only
captured failure signatures to closed reason codes. An unknown target response
is `uncertain_write`, not a guessed known failure.

`Adapter.execute` has one lawful call site:
`agent_runtime.gate.execute_authorised_adapter`. That helper accepts only a
currently leased, authenticated work receipt returned by the control API. The
banned-call check remains strict and gains a regression proving that an
`adapter.execute` call in any other module fails CI.

The framework retains PRD-09's future `finance` system value, but the v1
operation catalogue still contains only ICON/EDMS rows and registers no finance
executor. No adapter operation is a funds transfer. `icon.payment_voucher`,
`edms.claim_payment`, and `edms.payment_workflow` remain unregistrable
execution slots while PRD-12/GP-1 is closed.

## 4. Runtime and operation configuration

Add `packs/motor/projection/runtime.yaml`:

```yaml
version: 1
runner:
  heartbeat_seconds: 30
  lease_seconds: 120
  reaper_seconds: 60
  max_attempts: 3
  default_step_timeout_seconds: 20
  edms_step_timeout_seconds: 90
  upload_timeout_seconds: 480
  reflection_poll_seconds: 30
  reflection_timeout_seconds: 600
  screenshot_policy: before_and_after_every_step
  session_policy: isolated_context_per_run
control_api_auth:
  status: blocked_on_inputs
  blocked_on: runner-machine-identity
drift:
  schedule:
    status: pending_capture
    blocked_on: PRD-09-nightly-time
```

The PRD-specified 20s/90s/8min/30s/10min values are binding. The 30-second
heartbeat, 120-second lease, and three-attempt ceiling are the Packet-21
control-plane values. Values are pack data and fixture-overridable; code has no
fallback literals.

Validation rejects:

- missing/extra keys or non-integer/boolean-as-integer durations;
- heartbeat ≥ lease;
- lease < twice the heartbeat;
- reaper > lease;
- `max_attempts` other than the AR-1 ceiling 3;
- a step timeout exceeding its class ceiling;
- a screenshot/session policy other than the binding values;
- production runner auth marked live without an installed
  `RunnerAuthenticator`;
- a live drift schedule without a complete readback registry.

`operations.yaml` keeps all current production rows unchanged. A row may be
`mode=rpa,status=live` only when all are true:

1. its owner PRD permits activation;
2. its exact versioned click path passes the executable schema below;
3. its capability is currently L2 or higher;
4. the system adapter and runner authenticator are installed;
5. no runtime circuit breaker is open;
6. payment/GP-1 rules permit the operation.

PACKET-21's production file meets none of those activation conditions because
all paths remain deliberately uncaptured.

Add a fail-closed, environment-specific
`infra/rpa_runner/targets.yaml` registry. Target connectivity is deployment
configuration, not part of the signed claim product pack:

```yaml
version: 1
systems:
  icon:
    status: blocked_on_inputs
    blocked_on: open-item-1/open-item-2/open-item-12
    base_url: null
    secret_ref: null
  edms:
    status: blocked_on_inputs
    blocked_on: open-item-1/open-item-2/open-item-12
    base_url: null
    secret_ref: null
```

`base_url` and `secret_ref` are references, never credentials. A live target
must use HTTPS, match the runner's exact outbound allowlist, and name a Secrets
Manager reference. Missing/blocked targets refuse adapter construction. Fixture
targets are injected from the test directory and never modify the production
registry.

## 5. Full executable click-path contract

PACKET-21 extends the same YAML parsed by PACKET-20. There is still no second
field-order or browser registry.

Additional top-level keys:

```yaml
reconciliation: []
retry_probe: {}
known_failures: {}
```

Additional keys on every executable step:

```yaml
effect: read_only | local_input | external_write
timeout_class: default | edms | upload
write_id: null
postcondition: null
```

Example fixture shape:

```yaml
operation: edms.claims_workflow
version: 1.0.0
status: live
preconditions:
  - {assert: logged_in}
  - {assert: module, equals: Claims Workflow}
screens:
  - {id: workflow, label: Workflow, order: 1}
steps:
  - id: s1
    screen: workflow
    action: fill
    selector: "#claimNo"
    value: "{external.icon.claim_no}"
    value_kind: field
    external_encoding: raw
    effect: local_input
    timeout_class: edms
    write_id: null
    postcondition: {kind: exact_value, selector: "#claimNo"}
    paste_assist: {label: ICON claim number, copy: true}
  - id: s14
    screen: workflow
    action: click
    selector: "role=button[name='Submit']"
    effect: external_write
    timeout_class: edms
    write_id: submit_workflow
    postcondition: {kind: visible, selector: "#workflowReference"}
reconciliation:
  - step_id: s1
    selector: "#claimNo"
    normaliser: {kind: string_exact}
retry_probe:
  keys:
    - {from_step: s1, target: claim_number}
  exact_match: complete_without_write
  absent: retry_only_if_no_external_write_completed
  ambiguous: uncertain_write
known_failures: {}
failure_policy: screenshot_always, halt_on_selector_miss, no_guessing
```

Executable-definition rules:

- each selector must resolve to exactly one element; zero or multiple matches
  are `ui_drift`;
- the runner uses the captured selector exactly once and never searches for an
  alternative;
- every step declares its effect and timeout class;
- `external_write` requires a unique non-empty `write_id` and a captured
  postcondition;
- `read_only|local_input` must not carry `write_id`;
- every payload-backed `fill|select` step appears exactly once in
  `reconciliation`;
- every declared target output remains in `readback`;
- every definition containing an `external_write` has a retry probe naming
  only captured deterministic keys;
- a probe that can return more than one record must declare ambiguity and maps
  it to `uncertain_write`;
- every target representation has an explicit type normaliser;
- `failure_policy` must equal the PRD-09 literal;
- a live RPA definition missing any requirement fails startup;
- a pending production definition remains visible and does not fail startup.

A definition with zero `external_write` steps is read-only: it must still
reconcile every captured output, but it may be re-run after a stale lease
because it cannot mutate the target. The step-effect declarations, not an
operation-name heuristic, decide that classification.

Closed normalisers:

```text
string_exact
enum_exact
money_cents_exact
money_shillings_to_cents_exact
date_iso_exact
datetime_iso_exact
bool_exact
```

No implicit trimming, case folding, locale parsing, thousands separator,
currency prefix, timezone conversion, or rounding occurs. If a target displays
a different representation, its captured definition must add a versioned,
typed normaliser before activation.

## 6. AR-2 deferred execution and autonomy

Add one generic, backward-compatible deferred executor seam to
`agent_runtime`:

```python
runtime.register_deferred_executor(
    action_type,
    fn: Callable[[Action, str], DeferredAction],
)

runtime.execute_staged(
    action,
    *,
    run_id: str | None = None,
) -> object

runtime.finish_deferred(
    run_id,
    *,
    status: Literal["completed", "failed", "blocked"],
    outcome: dict[str, object],
    error: dict[str, object] | None = None,
) -> None
```

`DeferredAction` means the side effect is authorised and durably leaseable,
not completed. The gate leaves the existing `agent_run` running until the
projection coordinator records a reconciled terminal outcome. Existing
synchronous executors and Packet-01–20 behaviour do not change.

Register exactly one action type:

```text
projection.rpa.execute
```

Its action payload contains only:

```json
{
  "projection_id": "01...",
  "operation": "edms.claims_workflow",
  "definition_version": "1.0.0",
  "snapshot_hash": "..."
}
```

No target values, encrypted envelopes, selectors, credential refs, or blob keys
enter the staged review or `agent_runs`.

RPA sequencing:

1. a `mode=rpa,status=queued` projection is locked;
2. the service calls `execute_or_stage` with
   `capability_id=project.<operation>`;
3. L0 cannot occur for a live RPA row; startup refuses such configuration;
4. L1 remains paste-assist and creates no RPA action;
5. L2 creates one `DRAFT_RELEASE{subtype: projection_rpa}` confirmation;
6. approved L2 resolution resumes the exact staged `agent_run`;
7. L3/L4 authorise a deferred job immediately;
8. L3 applies the existing deterministic 20% `SAMPLE_REVIEW`;
9. the runner can pull only a row carrying the exact authorised run id;
10. reconciliation finishes the deferred run.

The closed review enum is unchanged. Add the `projection_rpa` subtype under
`DRAFT_RELEASE` with `DRAFT_RELEASE_PROJECTION@1`:

- **Approve:** launch the exact projection id/hash/version;
- **Edit→Approve:** change only this row to paste-assist, with a structured
  enum diff on `projection.mode`;
- **Reject:** launch nothing, record the reason, and make this row available
  in paste-assist;
- any changed id/hash/version, target value, or operation is 409;
- exact approval replay creates one leaseable job.

The subtype workspace shows claim context, operation, definition version,
snapshot hash, field-path/version counts, and the paste fallback option. It
never copies target values into the review event.

Add one exact projection COP step definition per canonical `project.*`
capability:

```text
authorise -> lease -> execute -> readback -> reconcile
```

Target-form step detail remains in `projections.evidence`; the AR-1 step list
holds only stable stage ids and refs. `G-PROC` checks the coarse sequence.

## 7. Outbound-only runner and control API

The runner is a client process. Its image:

- has no `EXPOSE`, listener, webhook, callback server, remote-debugging port,
  or inbound health endpoint;
- initiates outbound HTTPS only to the platform control API, the configured
  target system, AWS Secrets Manager, and the evidence upload endpoint;
- runs as non-root with a read-only root filesystem and writable ephemeral
  browser/tmp mounts only;
- pins Python, Playwright, and the browser revision;
- contains no production credential or selector file in the image;
- starts one isolated browser context per run and always closes it;
- never persists a target session, payload, screenshot, or credential to a
  durable local volume;
- is identical for the on-prem and Fargate host choices in ED-3a.

The platform exposes authenticated internal routes:

```http
POST /internal/projection-runner/jobs/claim
POST /internal/projection-runner/jobs/{projection_id}/heartbeat
POST /internal/projection-runner/jobs/{projection_id}/steps/{step_id}/evidence
POST /internal/projection-runner/jobs/{projection_id}/result
POST /internal/projection-runner/heartbeat
```

Production mounting requires an injected `RunnerAuthenticator`. No endpoint
accepts `X-Actor`, a console OIDC user token, query-string token, target-system
credential, or caller-supplied claim id. When the authenticator is absent,
construction reports `blocked_on_inputs: runner-machine-identity` and the
routes are not mounted.

The fixture authenticator exists only under tests. PACKET-22/infra supplies the
production machine-identity mechanism; PACKET-21 does not select mTLS, Entra
client credentials, or another unapproved scheme.

Claim response:

- selects one oldest authorised queued row for a system the runner declares;
- locks the projection;
- refuses when adapter health or the circuit breaker is not healthy;
- increments `attempts`;
- creates a 32-byte CSPRNG lease token, stores only its SHA-256 hash plus runner
  id, attempt, leased/expiry times, and run id under `evidence.rpa`;
- transitions to `executing`;
- returns the raw lease token once over TLS with the resolved click-path and
  ephemeral decrypted payload;
- logs every PII decrypt against `agent:projection-runner`;
- never stores the raw lease token or returns another claim's work.

Every callback supplies the raw lease token. The platform compares its hash in
constant time, verifies projection/runner/attempt/expiry, and rejects stale or
cross-run callbacks with 404/409 and no mutation. A valid heartbeat extends
expiry by exactly `lease_seconds`; it never changes attempt or authorisation.

Control-state key shapes are exact:

```text
projection.runner.<runner_id>
projection.circuit.<operation>
```

Unknown keys or an operation/version mismatch are ignored for execution and
reported as unavailable, never treated as healthy.

Runner heartbeats carry ids, versions, clocks, and closed health codes only.
They never contain screenshots, HTML, selectors, values, cookies, credentials,
or free-text target errors.

## 8. Lease, attempt, and crash-recovery semantics

The lease is a concurrency boundary, not permission to blind-retry.

At most one live lease exists per projection. The lease reaper runs every
minute from the pack configuration and handles a stale executing row:

1. inspect its durable last-completed step, effect class, write ids, and
   before/after evidence;
2. call the captured prior-completion probe through `Adapter.readback`;
3. if one exact target record matches the immutable snapshot, move to
   `verifying` and finish without another write;
4. if the probe proves absence and no `external_write` step completed, return
   to authorised `queued` for another attempt;
5. if the probe is ambiguous, unavailable after its bounded read policy, or
   any write may have occurred without exact completion proof, set `failed`
   and create `EXCEPTION{subtype: uncertain_write}`;
6. after three safe attempts, set `failed` and create
   `EXCEPTION{subtype: projection_attempts_exhausted}`.

The reaper never invokes `Adapter.execute`. It may only recover by exact
readback or issue a new lease that must pass the already-recorded gate
authorisation.

An `uncertain_write` review contains projection/operation/run/attempt ids,
write ids, last step, and evidence ids. It contains no guessed completion,
value, credential, selector, or automatic retry button.

## 9. Browser execution and evidence

Execution is deterministic:

1. fetch the target service-account secret by configured secret reference;
2. create a new browser context;
3. authenticate without recording credential fields;
4. assert each precondition exactly;
5. for each step, capture **before** screenshot, execute exact action, assert
   postcondition, capture **after** screenshot;
6. heartbeat before any long poll and at least every configured interval;
7. after the final write, call the declared `readback`;
8. close the context in `finally`.

Evidence key shape is server-owned:

```text
projection-evidence/<claim_id>/<projection_id>/rpa/<attempt>/<sequence>.png
```

`projections.evidence.rpa` stores a manifest:

```json
{
  "run_id": "01...",
  "attempts": [
    {
      "attempt": 1,
      "runner_id": "runner-fixture",
      "leased_at": "...",
      "ended_at": "...",
      "last_completed_step": "s14",
      "write_ids": ["submit_workflow"],
      "outcome": "submitted",
      "frames": [
        {
          "evidence_id": "01...",
          "step_id": "s1",
          "phase": "before",
          "sha256": "...",
          "captured_at": "..."
        }
      ]
    }
  ]
}
```

The API and events expose `evidence_id`, never a raw blob key. Authenticated
evidence reads resolve the server-owned key after same-claim RBAC, emit the
existing `pii.decrypted` access event with
`resource_type=projection_evidence`, projection/evidence ids, and actor, and
return private/no-store bytes. No new evidence-access event is invented.

Completion requires:

- one before and one after frame for every executed step;
- one failure frame for a failed selector/postcondition when the browser can
  still capture;
- matching SHA-256 between uploaded bytes and manifest;
- monotonically ordered sequence ids;
- no frame from another projection/attempt.

Credentials and login-form values are never screenshotted. Target screens may
contain claim PII; those screenshots are claim evidence, SSE-KMS protected in
production, role-checked, moved to Glacier Instant Retrieval at 90 days, and
deleted/crypto-shredded with the claim at seven years per ED-9.

The existing local `BlobStore` proves mechanics. Production S3/Object-Lock
certification remains register #30 and a launch gate.

## 10. Reconciliation and divergence

Every successful RPA write enters `verifying`; it cannot complete directly.

The platform:

1. validates readback keys against the exact operation version;
2. applies only the declared typed normalisers;
3. decrypts expected PII through claim core;
4. compares each declared target input and output to the immutable snapshot;
5. verifies required evidence completeness;
6. records one critical projection `G-VAL` result for immediate reconciliation;
7. completes only on an exact full match.

Exact match:

- store protected readback values plus paths/digests in `readback`;
- set `completed` and `completed_at`;
- emit one `projection.completed`;
- finish the deferred `agent_run`;
- for `icon.claim_register`, reuse PACKET-20's canonical field/cache/FSM
  finalisation without appending the field twice.

Mismatch:

- protect expected and actual values with the claim DEK in `divergence`;
- set `diverged` and `completed_at`;
- emit one `projection.diverged`;
- create one `EXCEPTION{subtype: divergence}`;
- record a critical G-VAL failure for immediate RPA or sampled-paste mismatch;
- let existing autonomy rules demote an L3+ capability;
- notify the assigned officer through the existing `projection.diverged` rule;
- never change the claim field, target value, or old payload.

`divergence` shape:

```json
{
  "schema_version": 1,
  "detected_by": "rpa_readback|paste_sample|nightly_drift",
  "detected_at": "...",
  "paths": [
    {
      "path": "reserve.total",
      "kind": "money",
      "expected": {"protected": "..."},
      "actual": {"protected": "..."},
      "expected_sha256": "...",
      "actual_sha256": "...",
      "evidence_ids": ["01..."]
    }
  ]
}
```

Events, reviews, notifications, ledger rows, and ordinary APIs carry only path,
kind, hashes, and evidence ids. The divergence workspace decrypts both values
only for an authorised actor and access-logs the decrypts.

Add `EXCEPTION{subtype: divergence}` with
`EXCEPTION_DIVERGENCE@1`. Resolution records one disposition:

```text
target_out_of_band
platform_snapshot_wrong
target_readback_wrong
unresolved
```

No disposition auto-corrects either side or reopens the old projection.
`platform_snapshot_wrong` becomes correction-corpus evidence through the
existing structured-diff loop. A corrected claim value or target retry requires
a new version/new projection under ordinary workflows.

Standing drift is not autonomy-failure evidence until a human disposition
attributes it to the platform; immediate RPA and sampled-paste mismatches are
critical evidence immediately.

## 11. Selector drift and paste-assist fallback

`ui_drift` means:

- a selector resolved zero or more than one element;
- a captured precondition/postcondition failed;
- the runner did not hunt, infer, or continue.

The response always:

1. halts the browser;
2. captures the failure frame when possible;
3. creates one `EXCEPTION{subtype: ui_drift}`;
4. opens a durable circuit breaker for the operation/version in
   `platform_state`;
5. makes S-6 and Systems show `circuit_open`.

If durable evidence proves no external-write step completed, the same
projection changes to `mode=paste_assist,status=queued`; the RPA attempt remains
in evidence and the operation's effective mode for new requests is
paste-assist.

If any write may have occurred, the projection becomes `failed` with
`uncertain_write`; it is **not** offered as paste-assist because a human must
first determine target state.

The signed pack file is never edited at runtime. The circuit breaker is a
runtime safety override. Clearing it requires an admin/claims-manager action
after:

- a strictly newer operation-definition version is installed;
- its synthetic selector/reconciliation suite passes;
- adapter health is healthy.

Clearing is ledgered. It affects new projections only; no terminal row is
reused.

## 12. PASTE_READBACK_CHECK resolution

PACKET-20 creates the weekly sample. PACKET-21 makes it actionable without
copying target values into review history.

Add:

```http
POST /console/reviews/{review_id}/paste-readback/capture
```

The authenticated officer submits exactly the target values declared by the
sampled projection and optional screenshot bytes. The service:

- verifies the open review and same projection;
- validates declared keys and types;
- protects values under the claim DEK;
- stores them in `evidence.paste_readback_checks` under a random `capture_id`;
- stores evidence through the same evidence manifest;
- returns only `capture_id`, mismatch paths, and hashes;
- never puts raw observed values in events, logs, or the review item.

Version `PASTE_READBACK_CHECK@2` requires `capture_id`, `capability_id`, and the
existing structured diff:

- **Approve:** allowed only when server comparison is exact;
- **Edit→Approve:** allowed only when mismatch paths exactly equal the
  server comparison; atomically transitions `completed→diverged`;
- **Reject:** requires a reason and creates
  `EXCEPTION{subtype: paste_readback_unavailable}` without inventing a match;
- capture/review replay is idempotent;
- a stale capture after projection/review change returns 409.

The v1 schema remains registered for replay of historical resolved items but no
new Packet-21 item resolves with it.

## 13. EDMS known-failure handlers

Implement only the two PRD-09 handlers, activated only by a complete operation
definition.

### Duplicate filename

For `edms.attach_and_tag`:

1. match only the captured duplicate-filename signature;
2. rename to `{original}__{claim_id_suffix}{n}` with `n=1`, where
   `claim_id_suffix` is the final six uppercase characters of the claim ULID;
3. retry the upload exactly once;
4. a second collision becomes
   `EXCEPTION{subtype: edms_duplicate_filename_collision}`;
5. no third name, random suffix, overwrite, or alternate folder is attempted.

The original/renamed filename may be sent to EDMS but is absent from ordinary
logs/events. Evidence carries digests and collision number only.

### Slow reflection

After a declared EDMS write:

- poll the captured search/readback path every 30 seconds;
- stop at exact match;
- stop at 10 minutes;
- timeout with no proof of absence or completion is `uncertain_write`;
- never resubmit merely because EDMS has not reflected yet.

Upload actions use the eight-minute step ceiling with progress polling.

Production failure signatures/selectors remain pending with open item 3; the
acceptance suite uses deterministic fixture signatures.

## 14. Standing drift

Add a named idempotent task:

```text
projection_agent.nightly_drift
```

The task reads only claims that have:

- a canonical external reference required by the drift definition;
- a prior completed RPA projection for that system/operation version;
- a live captured drift mapping;
- no unresolved divergence for the same projection/path.

`packs/motor/projection/drift.yaml` declares:

```yaml
version: 1
status: pending_capture
blocked_on: PRD-09-nightly-map-and-time
schedule:
  day_of_week: "*"
  hour: null
  minute: null
  timezone: Africa/Nairobi
checks:
  - id: icon_reserve_total
    status: pending_capture
    blocked_on: open-item-3
    source_operation: icon.reserve_create
    external_ref: external.icon.claim_no
    claim_path: reserve.total
    target_readback: null
  - id: icon_claim_status
    status: pending_capture
    blocked_on: open-item-3
    source_operation: icon.claim_register
    external_ref: external.icon.claim_no
    claim_path: null
    target_readback: null
```

The target claim-status mapping and nightly EAT time are not present in the
source documents. Production Beat registration remains visibly disabled until
PACKET-22 captures them. A complete fixture registry proves that a target edit
is detected within one invoked 24-hour cycle.

Drift comparison reuses the same normalisers and divergence lifecycle. Exact
repeat creates no duplicate event/review. A completed projection may become
diverged; it never silently returns to completed.

## 15. APIs and console

Extend the Claim-360 Systems API without breaking the five PACKET-20 routes:

```http
GET /console/claims/{claim_id}/projections/{projection_id}/rpa
GET /console/claims/{claim_id}/projections/{projection_id}/evidence/{evidence_id}
POST /console/reviews/{review_id}/paste-readback/capture
```

RPA view returns only:

- projection/operation/capability/definition/hash;
- gate state and review id;
- run id, attempt, lease health, current/last step;
- ordered evidence ids and authenticated URLs;
- reconciliation status and mismatch paths/hashes;
- circuit-breaker/fallback state;
- timestamps in UTC for the API, rendered EAT in UI.

The Systems tab keeps the exact seven Claim-360 tabs. Add:

- `Awaiting confirmation`, `Running`, `Reconciling`, `Fallback to paste`,
  `Failed`, and `Diverged` substate labels derived from evidence;
- progressive evidence frames for L2 watched runs;
- no optimistic completion;
- a loud uncertain-write/divergence state with review link;
- secure expected/actual detail only in the authorised divergence workspace;
- no selector, credential, cookie, raw blob key, or hidden PII in DOM data.

Replace S-6's placeholder with adapter/control rows:

```json
{
  "system": "icon",
  "configured_mode": "paste_assist",
  "effective_mode": "paste_assist",
  "status": "unavailable",
  "reason_code": "pending_capture",
  "runner_last_seen_at": null,
  "circuit_operation_ids": []
}
```

Admin and auditor may read; only admin/claims-manager may clear a qualified
circuit breaker. No credential or selector detail is returned.

Add S-4 series `projection_divergence_rate`:

```json
{
  "diverged": 1,
  "reconciled": 25,
  "rate_percent": 4,
  "basis": "current_projection_status"
}
```

Denominator = current rows in `completed|diverged`. Failed, queued, executing,
and verifying rows are excluded. When denominator is zero, `rate_percent` is
`null`, never a misleading zero. The tile is live, exportable, and pages via
the existing notification path when `diverged > 0`.

All routes are private/no-store/nosniff, server-role enforced, 404 across
claims, and auditor read-only.

## 16. Events, audit, grading, security, and retention

Use only existing events:

```text
projection.requested
projection.completed
projection.failed
projection.diverged
review.created
review.resolved
pii.decrypted
agent.action_logged
grader.failed
autonomy.demoted
```

Add `projection.diverged → projection.diverged` to the ledger action map.
`projection.failed` remains mapped. Lease/heartbeat/step callbacks update
durable evidence and AR-1 steps but do not invent event types.

Event payloads carry:

- projection/claim/operation/capability/run/attempt ids;
- definition version and snapshot hash;
- status/reason codes;
- step/write/evidence ids;
- mismatch paths/kinds and expected/actual hashes.

They never carry target values, PII envelopes, selectors, filenames, cookies,
credentials, target HTML, screenshot bytes/keys, or raw lease tokens.

Every completed RPA run receives:

- `G-PROC` against the declared coarse COP steps;
- critical `G-VAL` reconciliation evidence;
- existing L3 deterministic sample behaviour;
- correction-corpus capture from material human resolution.

Security:

- dedicated target service accounts only; never a person's login;
- Secrets Manager references only, no pack secret values;
- runner platform identity and target identity are separate;
- TLS 1.2+ on every runner connection;
- no production PII in fixtures;
- decrypts and evidence reads are access-logged;
- screenshots inherit claim retention and ED-9 lifecycle;
- portal/public routes and `lot_public` are untouched.

## 17. Protected backend acceptance

The Packet-21 backend suite pins:

1. no migration or projection-column change; 0015 remains the exact PRD DDL;
2. adapter/health/result enums and unknown-key rejection;
3. runtime config exact launch values and invalid-boundary failures;
4. production operation catalogue remains unchanged and non-executable;
5. fixture RPA activation refuses below L2, with missing auth, incomplete path,
   unhealthy adapter, open circuit, or GP-1-owned operation;
6. one deferred action crosses AR-2; no runner lease exists before L2 approval;
7. L2 exact approval resumes one agent run/job; edit/reject produces paste
   fallback and no external call;
8. L3 execution and 20% sampling reuse the existing deterministic selector;
9. the runner can pull only authorised rows and exactly one concurrent lease;
10. raw lease token is never stored; stale/wrong/cross-claim callbacks fail;
11. attempts increment only on lease and stop at three;
12. 14-field synthetic `edms.claims_workflow` writes in order, has complete
    before/after evidence, exact readback, one completed event, and zero
    divergence;
13. every Playwright context is isolated and closed on success/failure;
14. changed selector halts without hunting, captures failure, creates one
    `ui_drift` EXCEPTION, opens the circuit, and safely falls back only before
    a write;
15. selector failure after a possible write becomes `uncertain_write` and
    never offers automatic paste/retry;
16. duplicate filename renames once exactly; second collision creates one
    exception and no third attempt;
17. EDMS reflection polls at 30 seconds, stops by 10 minutes, and never
    resubmits;
18. killed runner before a write safely re-leases; killed after submit first
    probes target, recognises exact prior completion, and creates no duplicate;
19. ambiguous/unavailable retry probe creates `uncertain_write` and performs no
    execute;
20. readback exact type comparison completes; formatting not declared by a
    normaliser diverges rather than being guessed;
21. immediate mismatch stores protected values, emits one diverged event/review,
    records critical G-VAL, and never auto-corrects;
22. PII is absent from jobs-at-rest, events, reviews, agent runs, logs, health,
    ledger, and errors; authorised runner/deviation reads are logged;
23. sampled paste capture stores protected observed values, exact approval
    completes the review, mismatch performs `completed→diverged`, and repeat is
    idempotent;
24. nightly fixture detects a deliberate target edit within one invoked cycle
    and exact repeat creates no duplicate;
25. divergence rate excludes non-reconciled rows and returns null at denominator
    zero;
26. `projection.diverged` is ledgered and existing notification rules receive
    one event;
27. approval-pack items 12/13 remain upload/pending-integration and never resolve
    from screenshots;
28. all values in money paths remain integer KES cents; target conversion is
    exact and no float enters a signature.

The PostgreSQL tier additionally runs concurrent lease, callback, circuit,
sample-resolution, and divergence-deduplication cases with real row locks.

## 18. Protected runner and console acceptance

Runner-contract tests pin:

1. image has no exposed/listening port and runs non-root;
2. outbound allowlist is platform + target + Secrets Manager/evidence only;
3. pinned Playwright/browser versions and deterministic install;
4. no secret/production selector copied into the image;
5. read-only root filesystem contract and ephemeral browser storage;
6. exact selector use; zero/multiple matches halt;
7. credential controls are excluded from screenshots;
8. target values/cookies do not enter runner logs;
9. browser context closes after success, target failure, callback failure, and
   process cancellation.

Console tests pin:

1. seven Claim-360 tabs remain exact;
2. L2 confirmation, running, reconciling, fallback, failed, and diverged states;
3. evidence frames arrive in order without a page reload and remain
   keyboard/screen-reader usable;
4. no optimistic completion and stale callback recovery;
5. divergence expected/actual values require authorised detail access and are
   absent from list DOM;
6. paste readback capture + exact/mismatch/reject resolution;
7. S-6 adapter health replaces the unavailable placeholder without exposing
   secrets/selectors;
8. qualified circuit reset and forbidden-role 403;
9. S-4 divergence tile, null denominator, CSV export, and non-zero alert state;
10. axe pass and usable 1366×768 layout.

## 19. Live/manual gates not discharged

PACKET-21 does not discharge:

- service-account creation (open item 1);
- DG-1/browser accessibility or ICON test instance (item 2);
- ICON/EDMS path/value-map/failure-signature capture (item 3/17);
- runner host selection (item 12);
- production runner machine identity;
- S3/KMS/Object-Lock certification (item 30);
- any real replay, selector injection, duplicate filename, crash, or drift test;
- the five-claim/14-field live `edms.claims_workflow` acceptance;
- the first-25 watched-run posture when no test instance exists.

Those are PACKET-22/infra/live-gate work. Packet-21 acceptance reports
`synthetic_control_plane_green`, never `rpa_live`.

## 20. CTO decisions and ED-11 register entries

Builder appends #276–#296 with the implementation PR:

- **#276 — control-plane persistence.** Reuse exact projections, agent_runs,
  platform_state, and blob-store boundaries; no job/lease/health table or
  migration.
- **#277 — runner platform authentication.** Add an injected machine
  authenticator; no X-Actor/user-token fallback and no production route mount
  until infra supplies it.
- **#278 — remote AR-2 execution.** Add one deferred gate executor contract so
  authorisation and actual reconciliation share one durable agent run; only the
  gate helper may call `Adapter.execute`.
- **#279 — RPA starts at L2.** A live RPA definition below L2 is invalid. Reuse
  DRAFT_RELEASE with a projection subtype for one-click launch/fallback; add no
  eighteenth review type.
- **#280 — auto-fallback without pack mutation.** Persist a versioned runtime
  circuit breaker in platform_state; safe pre-write failure reuses the row as
  paste-assist, possible-write failure is uncertain and terminal.
- **#281 — write-boundary/retry gap.** Every executable step declares effect,
  write id, postcondition, reconciliation, and prior-completion probe; missing
  metadata blocks activation.
- **#282 — runner timing.** Pack owns heartbeat/lease/reaper limits; PRD
  timeouts remain exact, heartbeat=30s, lease=120s, safe attempts=3.
- **#283 — crash recovery.** Reconcile exact prior completion before any
  re-lease; proven absence before any write permits retry, ambiguity becomes
  uncertain_write.
- **#284 — evidence identity.** Store server-owned evidence ids/digests in
  projection JSON and resolve hidden blob keys through same-claim RBAC; no new
  artifact table.
- **#285 — target normalisation.** Closed explicit type normalisers only; no
  implicit trim/case/locale/rounding. Complete reconciliation mapping is
  required for RPA activation.
- **#286 — protected divergence.** Expected/actual values use the claim DEK;
  events/reviews expose paths/hashes/evidence only; legal edges include
  verifying/completed to diverged; no auto-correction.
- **#287 — sampled paste capture.** Two-step protected capture + v2 opaque
  resolution prevents target values entering review history and owns
  completed→diverged on mismatch.
- **#288 — EDMS failures.** Duplicate signature must be captured, rename once
  exactly, second collision fails; slow reflection polls readback and never
  resubmits.
- **#289 — adapter health.** Derive health from captured availability, runner
  heartbeat, adapter probe, and circuit state in platform_state; S-6 exposes
  codes/times only.
- **#290 — nightly drift gap.** Build the task/registry but leave production
  time, ICON status mapping, and target selectors pending; fixtures prove the
  within-24h mechanic.
- **#291 — divergence metric.** Point-in-time denominator is completed+diverged;
  zero denominator returns null; failed/in-flight rows are excluded.
- **#292 — approval-pack boundary.** Screenshots are audit evidence, not
  approval-pack projection artifacts; items 12/13 stay officer-upload/
  pending-integration until PACKET-22 captures an explicit artifact output.
- **#293 — deterministic EDMS suffix.** `claim_id_suffix` was undefined; use
  the final six uppercase claim-ULID characters and retry with `n=1` only.
- **#294 — owner-side fixture transition.** Packet-20 deliberately pins no
  divergence ledger mapping/executor/extra routes and Packet-12 pins the S-6
  placeholder. PACKET-21 is authorised to update those exact assertions to the
  new closed map, sole deferred executor, eight console routes, and typed
  unavailable health rows without weakening the no-funds/no-secret checks.
- **#295 — evidence access audit.** Reuse `pii.decrypted` with a typed
  `projection_evidence` resource payload for every authenticated screenshot
  read; no direct ledger append or fifth projection event is invented.
- **#296 — target endpoint/secret slots.** Add blocked ICON/EDMS target records
  with nullable HTTPS endpoint and Secrets Manager reference; no environment
  guess or embedded credential. Fixtures inject their own targets.

## 21. Builder guardrails

- **No production activation:** production `operations.yaml` stays unchanged.
- **AR-2:** no adapter execution without the deferred gate authorisation.
- **No blind retry:** a possible write without exact readback is
  `uncertain_write`.
- **Never hunt:** exact selector cardinality one or halt.
- **Never guess:** missing effect, normaliser, retry key, signature, schedule,
  status map, auth, or credential is a blocker.
- **No payment execution:** no transfer/release/send-funds action or GP-1
  bypass.
- **Append-only:** no current claim field is updated in place; old projections
  remain failed/diverged.
- **Money:** integer cents in snapshots/comparison; exact declared boundary
  conversion only.
- **PII:** ephemeral runner delivery, claim-DEK projection storage, access-logged
  decrypts, evidence under claim retention.
- **Closed review enum:** use DRAFT_RELEASE/PASTE_READBACK_CHECK/EXCEPTION
  subtypes only.
- **Single ledger writer:** event mapping only; no direct ledger write.
- **Config over code:** timeouts, leases, schedules, mappings, selectors,
  normalisers, failure signatures, and secret refs are data.
- **No pack mutation:** runtime fallback uses the control-state registry.
- **No public surface:** no portal/lot whitelist or unauthenticated evidence
  route.
- **No protected regression rewrite:** prior acceptance retains its meaning.

## 22. Definition of done and hand-off

- ruff, money lint, banned-call lint, OpenAPI check, full SQLite and PostgreSQL
  tiers green;
- backend pooled statement+branch ≥80%; frontend ≥70%; pack calcs remain 100%;
- no migration diff and exact 0015 schema snapshot green;
- runner-contract tests green from a clean container build;
- generated OpenAPI contains the three authenticated console routes and the
  internal runner contract only when a test authenticator is injected;
- production application reports runner/drift blocked rather than mounting an
  insecure path;
- G-PROC and G-VAL coverage registered for all projection execution outputs;
- runbook covers gate staging, lease inspection, safe reaping, uncertain writes,
  circuit open/reset, evidence access, reconciliation, divergence resolution,
  paste sampling, drift, health, and total runner shutdown;
- Admin health, Systems RPA progress, divergence workspace, and portfolio tile
  pass accessibility and 1366×768 acceptance;
- production operation catalogue diff is empty apart from comments or explicitly
  approved non-executable runtime refs;
- PR description calls out #276–#296, synthetic-only acceptance, absence of a
  migration, container digest, coverage, and every live blocker.

Builder order:

1. adapter/result/runtime config contracts;
2. deferred agent-runtime seam + strict banned-call regression;
3. projection lifecycle/evidence helpers;
4. runner auth + lease/heartbeat/result API;
5. outbound container + exact browser executor;
6. readback/reconciliation/divergence;
7. selector circuit + crash recovery;
8. EDMS known failures;
9. paste-readback resolution + drift;
10. health, Systems, divergence workspace, and portfolio tile;
11. grader/ledger/notify/OpenAPI/runbook;
12. synthetic, PostgreSQL, container, console, and full regression.

PACKET-22 consumes unchanged:

- exact adapter/health/result contracts;
- full executable click-path schema;
- runtime timings and runner container;
- deferred AR-2 execution receipt;
- projection lease/evidence/reconciliation shapes;
- circuit-breaker and health surfaces;
- secure divergence/paste-sample workspaces;
- drift registry/task;
- all synthetic acceptance fixtures as contract tests.

Stop and append a new ED-11 register entry before making any choice not fixed
above. Do not activate a production operation or solve a DG/live dependency in
this packet.
