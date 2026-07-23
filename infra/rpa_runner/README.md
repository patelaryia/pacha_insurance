# RPA runner container

The outbound-only runner that executes captured ICON/EDMS click paths on behalf
of the PRD-09 projection control plane. Built by PACKET-21; **not activated**.

## What it is

A client process. One cycle is:

```
claim → execute in one isolated browser context → upload evidence → post result
```

Every call is an outbound HTTPS request the runner initiates. The platform never
calls in.

## Contract (pinned by `tests/acceptance/test_packet_21_runner_contract.py`)

| Property | Value |
| --- | --- |
| Inbound surface | none — no `EXPOSE`, listener, webhook, callback server, remote-debugging port, or health endpoint |
| Outbound allowlist | platform control API, configured target system, AWS Secrets Manager, evidence upload |
| User | non-root (`uid 10001`) |
| Root filesystem | read-only root filesystem; only `/var/run/pacha-browser` and `/tmp/pacha` are writable, and both are ephemeral |
| Pinning | Python, Playwright, and the browser revision are all pinned |
| Secrets | none in the image; Secrets Manager *references* only |
| Selectors | none in the image; the click path arrives in the claim response |
| Session | one isolated browser context per run, always closed in `finally` |
| Host | identical for the on-prem Docker and AWS Fargate choices in ED-3a |

## Why it does not start

`entrypoint.py` exits `78` (`EX_CONFIG`) with a structured blocker list. Today
every blocker applies:

* `target-icon` / `target-edms` — no endpoint or service account has been
  captured (open items 1/2/12), so `targets.yaml` is `blocked_on_inputs`;
* `runner-machine-identity` — PACKET-21 deliberately does **not** choose mTLS,
  Entra client credentials, or any other scheme. Infra supplies it in PACKET-22;
* `control-api-url` — no environment endpoint is configured.

The platform side matches: with no injected `RunnerAuthenticator`, the internal
`/internal/projection-runner/*` routes are not mounted at all, and the
application reports `blocked_on_inputs: runner-machine-identity`.

## Environment

| Variable | Meaning |
| --- | --- |
| `PACHA_CONTROL_API_URL` | HTTPS base URL of the platform control API |
| `PACHA_RUNNER_IDENTITY` | the machine identity material reference (PACKET-22) |
| `PACHA_RUNNER_ROLE` | fixed at `projection-runner` |

## Build

```
docker build -t pacha/rpa-runner:packet-21 infra/rpa_runner
```

The image digest is recorded in the PR description. PACKET-21 publishes nothing.

## What this container will never do

Execute a funds transfer, release a payment, retry a write it cannot prove did
not happen, search for an alternative selector, persist a target session or
screenshot to a durable volume, or accept an inbound connection.
