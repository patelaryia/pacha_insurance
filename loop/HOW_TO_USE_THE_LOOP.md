# How to use the Pacha build loop

This guide is for the product owner. You do not need to understand the code
inside the loop to operate it.

## What the loop does

The loop turns an approved product requirement into a reviewed pull request:

1. **Claude acts as the CTO.** It breaks the requirement into a small,
   testable work packet and later reviews the finished work.
2. **Codex acts as the builder.** It receives one packet, writes the code and
   tests, and commits its work on a dedicated branch.
3. **The controller acts as the referee.** It checks the evidence, watches CI,
   sends failed work back, and decides whether the work can progress.
4. **You remain the owner.** The loop interrupts you when a specification is
   unclear, a safety-sensitive area is touched, or repeated attempts fail.

The agents cannot mark their own work as complete. The controller makes every
status change from independently checked evidence.

## The normal routine

### 1. Add work

Ask Claude to slice an approved PRD:

```text
/slice <the PRD or feature you want built>
```

Claude will produce one or more packet files. Read the plain-English scope and
the acceptance criteria. Approving a packet means approving what will be built
and how completion will be judged.

### 2. Let the loop run

Once a packet is queued, no further prompt is needed. The scheduled controller
will:

- give it to Codex;
- check Codex's result and local tests;
- open or update a pull request;
- wait for GitHub CI;
- ask Claude for an independent review;
- send blocking findings back to Codex; and
- merge eligible routine work when the two-identity merge lane is enabled.

### 3. Check progress

For a quick status:

```bash
./loop/controller.py status
```

For the owner summary:

```bash
cat loop/runs/digest.md
```

The digest answers four questions:

- What needs my decision?
- What is blocked?
- What did the reviewer approve with reservations?
- What has merged?

A recoverable copy of the same state is also kept on the
`automation/loop-state` branch.

## What the statuses mean

| Status | Plain-English meaning | What you do |
|---|---|---|
| `queued` | Ready, or waiting for an earlier packet | Nothing |
| `building` | Codex is working | Nothing |
| `awaiting_ci` | GitHub is running the full checks | Nothing |
| `review` | CI passed and Claude is reviewing | Nothing |
| `rework` | Claude or CI found a fix Codex can make | Nothing |
| `merge_ready` | Approved and safe, but waiting for a human merge | Merge it, or enable the two-identity lane |
| `merged` | Complete | Nothing |
| `blocked` | The retry or safety limit was reached | Read the reason and decide whether to re-slice |
| `escalated` | A human judgement is required | Read the reason and make the product decision |

## When the loop asks for you

The loop interrupts you deliberately in these cases:

- the PRD and its acceptance test appear to disagree;
- the requirement leaves no safe answer;
- a migration, money rule, authorisation rule, PII boundary, or other
  high-impact area was touched;
- the reviewer believes the packet itself was sliced incorrectly;
- a pull request changed after it was validated;
- the retry limit was reached; or
- the environment or credentials are not healthy.

Do not tell the agents simply to “try again.” Resolve the reason shown in the
digest, then update or re-slice the packet. This avoids paying for repeated
attempts against an impossible definition of done.

## Pause and resume

Pause before changing PRDs, acceptance tests, packet definitions, or loop
policy:

```bash
echo "why I am pausing" > loop/PAUSED
```

The controller will continue reporting status but will dispatch no new work.

When the change is reviewed, committed, and the acceptance oracle is current,
remove `loop/PAUSED`. The next scheduled tick resumes normally.

Before resuming, run:

```bash
./loop/controller.py preflight
./loop/controller.py oracle
./loop/controller.py tick --dry-run
```

All three should finish without an unexplained warning.

## If the laptop or controller crashes

Run:

```bash
./loop/controller.py recover
```

This restores an empty local ledger from the audit branch when necessary,
checks GitHub for what actually happened, and safely re-queues an interrupted
build after its lease expires.

Do not delete `loop/state.db` as a routine troubleshooting step. Recovery is
for genuine loss or corruption, not for resetting an inconvenient status.

## The important safety promises

- Codex cannot change the tests that define success.
- Codex cannot change the loop, packet, CI, or governance rules in its own PR.
- Claude reviews the exact commit that passed CI from a clean, separate
  checkout.
- Neither agent receives the controller's GitHub or Pacha credentials.
- The loop never executes a payment.
- High-impact changes always come back to you.
- A missing fact never becomes a guessed value.
- Runtime records never create commits on your development branch.

## Before enabling unattended merges

Unattended merging needs two real GitHub identities:

- a builder identity that pushes and opens the pull request; and
- a separate reviewer identity that approves and merges it.

The controller checks the identities rather than trusting two different token
names. Until both are configured and `auto_merge` is enabled, approved routine
work stops safely at `merge_ready`.

Even after unattended merging is enabled, blast-radius work still stops for
you. That is a permanent safety boundary, not a temporary limitation.

## A good operating rhythm

- Check the digest once each working day.
- Review escalations before adding more packets.
- Keep packets small enough to explain in one sitting.
- Pause before changing the definition of done.
- Treat repeated failures as a slicing problem, not an invitation for more
  retries.
- Review the weekly drift report for architecture that has moved away from the
  PRDs.

The intended experience is quiet: routine work progresses on its own, while
the loop brings you only decisions that genuinely require the owner.
