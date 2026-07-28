# Pacha build-loop operator runbook

The worker runs one durable lifecycle for one reviewed packet. Internal CI
polls, retries, reviews and rework do not create Codex tasks.

## Activate one packet

Start from current `origin/main` in a clean operator checkout. Commit only the
reviewed packet, its named acceptance tests and the refreshed
`loop/oracle.lock`. The acceptance tests may deliberately be red before the
implementation, so this owner-activation commit is not merged by itself.

When `start` runs, the controller pins current `origin/main` and the activation
commit. It refuses any product-source change in that commit. On the first
builder attempt it seeds only the packet, named tests and oracle into the
packet branch as a controller-owned commit; the builder cannot alter them.
The final packet PR therefore contains the immutable owner contract and the
implementation together and can pass the mandatory CI gate without merging a
red test into `main`.

## Start the worker

From the repository root:

```bash
git fetch origin main
LOOP_PYTHON=.venv/bin/python .venv/bin/python loop/controller.py preflight
LOOP_PYTHON=.venv/bin/python .venv/bin/python loop/controller.py oracle
LOOP_PYTHON=.venv/bin/python .venv/bin/python loop/controller.py start TEMPORAL-T04
LOOP_PYTHON=.venv/bin/python .venv/bin/python loop/controller.py worker TEMPORAL-T04
```

Keep the `worker` command under the machine's normal service supervisor for
unattended operation. Restarting it resumes the same active lifecycle and
persisted backoff.

## Monitor

```bash
.venv/bin/python loop/controller.py status
.venv/bin/python loop/controller.py notifications TEMPORAL-T04
cat loop/runs/digest.md
```

The worker emits only material updates: started, rework needed, blocked or
escalated, and completed. `completed` means required CI is green, the reviewer
approved the explicit packet criteria, and the merge/completion gate passed.
If an owner merge is required, leave the worker running; it emits the blocker
once and completes the same lifecycle when GitHub reports the merge.

## Stop

An interrupt stops only the worker process; the active lifecycle resumes when
the worker restarts. To make an explicit owner stop:

```bash
.venv/bin/python loop/controller.py stop TEMPORAL-T04 \
  --reason "owner is changing the packet"
```

That records the terminal outcome `blocked_owner`. Authentication, network and
other preflight failures do not need a stop or restart: the worker records one
deduplicated blocker, backs off to the configured ceiling, and resumes
automatically when preflight recovers.
