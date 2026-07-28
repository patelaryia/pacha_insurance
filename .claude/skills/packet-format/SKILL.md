---
name: packet-format
description: Use when slicing a PRD into packets or reading the packet board - the front-matter schema, what a well-sliced packet looks like, and the two ways slices go wrong
---

# Packet format

A packet is one branch, one PR, one independently testable unit of work.
Packet files live in `docs/packets/`.

A packet file is the **spec** plus historical bootstrap state. The
authoritative runtime state is `loop/state.db`, a SQLite ledger the controller
owns and publishes to a dedicated audit branch. Runtime state is never written
back into the packet on a development branch. Agents never write state.

That split exists because the builder runs in a git worktree. "The packet
file is the source of truth" silently means two files on two filesystems the
moment you do that, and an earlier version of this loop lost every builder
escalation to exactly that ambiguity.

## Front matter

Two halves. The **spec** is written by the slicer and never by an agent. The
historical fields seed an empty ledger once so adopting the loop does not
resurrect completed work. After registration, editing them changes nothing.

```yaml
---
# --- spec: the slicer writes this, agents never do -----------------
id: PACKET-19                    # PACKET-NN or TEMPORAL-TNN. Unique.
prd_ref: docs/PRD-08_Approval_Pack_Generator_v1.1.md §8.2, §8.5–§8.7
title: Approval-note review, crash-safe signing, and authority routing
depends_on: [PACKET-18]          # ids on this board. Honest, or parallelism is unsafe.
branch: codex/packet-19-approval-workflow
blast_radius: true               # true if the diff touches loop/blast-radius.yml
acceptance_tests:                # also pinned by hash in loop/oracle.lock
  - tests/acceptance/test_packet_19_approval_workflow.py
  - tests/acceptance/console/test_packet_19_console.test.tsx

# --- bootstrap history: never updated by a running controller ----------
status: queued
pr: null
attempts: 0
reason: null
---
```

### status

The controller owns every one of these. An agent that writes a status is
writing to a mirror.

| status | meaning | set by |
|---|---|---|
| `queued` | dispatchable once dependencies are merged | slicer, controller |
| `building` | leased; a builder is running against a pinned base SHA | controller |
| `awaiting_ci` | PR open, controller polling the six required checks | controller |
| `review` | CI **green**, waiting on the reviewer | controller |
| `rework` | reviewer findings or red CI sent it back. **Dispatchable** | controller |
| `merge_ready` | approved, green, routine — waiting on a merge | controller |
| `merged` | reconciled from GitHub, never asserted locally | controller |
| `blocked` | a breaker tripped; `reason` required | controller |
| `escalated` | needs a human decision; `reason` required | controller |

`rework` being dispatchable is load-bearing. An earlier version of this loop
set `rework` and then only ever selected `queued`, while counting `rework` as
an occupied concurrency slot — so the first requested rework stopped the
whole loop indefinitely.

`merge_ready` and `escalated` are both "waiting on you", and the difference
matters: `merge_ready` means nothing is wrong, `escalated` means something
needs deciding.

## Body

Free prose, but four things must be present and unambiguous:

1. **What to build** — the executable contract. Endpoints, schemas, events,
   states, exact status codes, exact error identifiers.
2. **Constraints** — what it must consume unchanged from earlier packets.
3. **Explicit non-goals** — what belongs to the next packet. The most
   valuable paragraph in the file, and the one most often missing.
4. **Acceptance** — what the named tests pin, restated so a builder can
   check itself before running them.

The builder writes nothing into the packet file. If it stops, it writes
`.loop/result.json` inside its own worktree — the only channel the
controller reads — and the controller records the escalation in the ledger
and in `reason`.

## The four rules a slice must satisfy

1. **One branch, one PR, independently testable.** If it needs two PRs, it
   is two packets.
2. **It names existing acceptance tests.** If they do not exist, the slicer
   writes them first, as failing tests, and commits them **separately**.
   `tests/acceptance/` is CODEOWNERS-protected, so that commit needs the
   owner's approval before any packet can build against it. That is a
   feature: the definition of done gets a human read before anything is
   built to satisfy it.

   After slicing, run `loop/controller.py oracle --update` to pin the new
   files by content hash in `loop/oracle.lock`. The controller refuses to
   dispatch a packet whose named tests are not pinned, and re-checks every
   hash in the builder's worktree before opening a PR. Directory names are
   not the boundary — the hash list is. New unit and integration tests the
   builder writes to satisfy ED-7 are unaffected.
3. **`depends_on` is honest.** Under-declaring is what makes parallel
   dispatch unsafe. Over-declaring only costs serialisation. When unsure,
   declare it.
4. **`blast_radius` reflects `loop/blast-radius.yml`.** The controller
   re-derives it from the actual committed diff before opening a PR and
   corrects a `false` upward, so under-declaring buys nothing.

**A packet that cannot be specified to this standard is not sliced.** It is
flagged for the owner with the reason. Shipping a vague packet to a builder
does not make the vagueness go away; it moves it into a diff.

---

## A good packet: PACKET-18

`docs/packets/PACKET-18_approval_pack_backend.md` is the model. What makes
it good:

- **Its non-goals are executable.** It does not merely say "signing is out
  of scope" — it says attempting to resolve its own `NOTE_REVIEW` returns
  `409 NOTE_REVIEW_UI_NOT_BUILT`, changes no row, and emits no FSM
  transition. "There is therefore no accidental back door to
  `IN_APPROVAL`." A builder cannot accidentally overshoot, because
  overshooting has a specified failure.

- **It states the slice boundary and defends it.** §0 explains that merge
  and cited-note generation ship together because "a merge without its cited
  note consumer would leave an ungraded artifact seam". The reviewer can
  check the boundary against a stated reason instead of a preference.

- **It builds the slot, never the value.** C-08 is `blocked_on_inputs` and
  T-03 is `pending_capture`, so the packet specifies the *visible blocked
  state* and forbids inventing either value. This is guide §6 done properly.

- **It names its dependency and its consequence.** "Depends on PACKET-17
  merged and green, including owner correction #218 … it is not mergeable
  before that dependency."

- **Its acceptance tests exist before the build**, protected, and failing by
  design.

## A bad packet: PACKET-19, and the original 18/19 split

Two different failures, both real, both from this board.

### PACKET-19 is one PR of work only on paper

Read its §12 hand-off: nine sequential implementation stages, from subtype
schemas through to a React split-pane editor with fake-timer autosave tests.
It spans two runtimes, two test frameworks, fifteen backend acceptance
assertions and six console ones, and its own §8 replaces three console
surfaces.

It violates rule 1. The honest cut is at the §7/§8 line — backend contract
and durable finaliser in one packet, console workspaces in a second that
depends on it. Both halves are independently testable; the acceptance files
are already separate (`test_packet_19_approval_workflow.py` and
`test_packet_19_console.test.tsx`), which is the board telling you where the
seam is.

The tell: **a hand-off section that is a numbered build order is a packet
that knows it is two packets.** A single-PR packet does not need to be told
what order to build itself in.

### The original 18/19 split was cut in the wrong place

PACKET-18 §0 records that a previous slice put the immutable merge in one
packet and the cited T-01 note generator in the next. That split passes a
naive size test and fails the real one: it would have merged an artifact
whose integrity gate lived in a packet that did not exist yet.

The rule this yields: **cut at integrity boundaries, not at size.** A slice
is wrong if either half can merge in a state the spec forbids the system to
be in. Ask of every proposed cut: *if only the first half merges, is the
system in a state the PRD permits?* If no, the cut is in the wrong place —
even if the resulting packet is large.

Note that these two failures pull in opposite directions, and that is the
whole job. Rule 1 says cut smaller; the integrity rule says do not cut here.
When they genuinely conflict and no cut satisfies both, the packet is not
sliceable — flag it for the owner rather than picking.
