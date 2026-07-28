---
description: Slice a PRD into packet files on the board. Owner-invoked, never scheduled.
argument-hint: <path to PRD> [optional scope note]
allowed-tools: Read, Write, Edit, Glob, Grep, Bash(git *), Bash(python3 loop/controller.py *)
---

# /slice — cut a PRD into packets

Input: `$1`, a PRD path. Optionally `$2`, a scope note narrowing what to cut.

Output: packet files in `docs/packets/`, `status: queued`, plus a report to
the owner. You are invoked by a human and your output is read by a human.
You do not run on a schedule and you do not dispatch anything.

Read `.claude/skills/packet-format/SKILL.md` first. It is the schema and the
standard; this file is only the procedure.

## Procedure

**1. Read the PRD and its authorities.** The PRD, plus
`docs/AGENT_BUILD_GUIDE.md`, `Section_0`, `Section_0.5`, and the open-items
register (`docs/Phase_3_Sequence_and_Open_Items_v1.1.md`). Earlier wins on
conflict.

**2. Read the board.** `python3 loop/controller.py status`. You need to know
what is already merged, what ids are taken, and which contracts a new packet
must consume unchanged rather than reimplement.

**3. Find the integrity boundaries before you find the sizes.** Ask of every
candidate cut: *if only the first half merges, is the system in a state the
PRD permits?* If no, the cut is wrong — see the PACKET-18 §0 case in the
packet-format skill. Cut at integrity boundaries; then, within those, cut to
one branch and one PR.

**4. For each packet, write the four required body sections** — what to
build, constraints, explicit non-goals, acceptance. Non-goals must be
executable: specify what happens when a builder oversteps (a status code, a
refused render, an unchanged row), not merely that it should not.

**5. Name the acceptance tests.**

- If they exist, list the paths.
- If they do not, **write them first as failing tests** and commit them as a
  separate commit touching only `tests/acceptance/`. Say so in your report:
  that path is CODEOWNERS-protected, so the owner must approve that commit
  before any packet can build against it.
- Never write a test you are not confident encodes the PRD's assertion
  exactly. A wrong test is worse than a missing one, because the loop will
  now build to it.

**6. Emit `depends_on` honestly.** Under-declaring is what makes parallel
dispatch unsafe. Over-declaring only costs serialisation. Unsure → declare.

**7. Set `blast_radius`** by comparing the packet's expected file surface
against `loop/blast-radius.yml`. The controller re-derives it from the
real committed diff before opening a PR and corrects `false` upward, so
under-declaring buys nothing.

**8. Validate and pin.** `python3 loop/controller.py oracle --update` must
succeed before you report — it fails loudly if a packet names an acceptance
test that does not exist, which is the most common way a slice is not
actually finished.

## When not to slice

**A packet that cannot be specified to the packet-format standard is not
sliced.** Do not ship a vague packet. Do not pick a value the PRD does not
give you in order to make a packet specifiable — that is the guessing the
whole system exists to prevent.

Flag it for the owner instead, in your report, with:

- what is underdetermined;
- the specific PRD section and open-items register item, if any;
- what a decision would unblock;
- what the narrowest safe behaviour would be if the owner wants it shipped
  blocked (the slot, the status, and the visible blocked state — never the
  value).

Flagging three packets and slicing two is a better run than slicing five,
one of which quietly invents a threshold.

## Report to the owner

End with, and nothing else:

1. **Sliced** — id, title, depends_on, blast_radius, acceptance tests, and
   one sentence on why the cut is where it is.
2. **Tests written** — paths, and the reminder that the commit needs owner
   approval.
3. **Not sliced** — each flagged item and what decision it needs.
4. **Dependency shape** — what can run in parallel and what is forced
   serial, so the owner can sanity-check `max_concurrent_packets`.
