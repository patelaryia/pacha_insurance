# Temporal reliability spike

This is an isolated, non-production spike for ADR-001. It imports no Pacha
production package, changes no production dependency, and contains no secret or
real claim data.

The representative flow is:

```text
opaque trigger id
  → automated Activity
  → heartbeating Activity
  → second automated Activity
  → durable timer
  → human Signal wait
  → execute_or_stage governed external action
  → authoritative database completion
```

Workflow payloads contain only `run_ref`, `claim_ref`, event/review references,
hashes, statuses and durations. Activities load the synthetic claim facts from
the Pacha-side store. A Payload Codec encrypts even the control-only payloads.

## Local run

Python 3.12 is required.

```bash
cd spikes/temporal_reliability
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pytest -q
```

The Temporal test server is downloaded by the SDK on first run. These tests
prove local mechanics only; they do not pass the Temporal Cloud gate.

## Cloud run

Copy `cloud.env.example` to a secret environment outside source control and provide
an approved synthetic-only Temporal Cloud namespace. Run:

```bash
.venv/bin/python -m temporal_spike.cloud_trial
```

The cloud command refuses to run unless the exact region, namespace endpoint,
TLS material, AWS-origin label and report output path are provided. It never
accepts claim facts. The cloud runner remains intentionally blocked until those
dependencies exist; see `docs/architecture/temporal_reliability_report.md`.

## Evidence rule

Do not mark an item `pass` from code inspection or synthetic mocks. Local test
results are labelled `local`. Cloud-region latency, service outage recovery,
Worker Deployment versioning, cost, SLA, DPA/DPIA and procurement require
external evidence.
