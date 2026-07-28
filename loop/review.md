---
description: Review a packet at status=review and return a JSON verdict to the controller.
argument-hint: <packet id, e.g. PACKET-19 or TEMPORAL-T03>
allowed-tools: Read, Glob, Grep, Bash(git diff *), Bash(git log *), Bash(gh pr *), Bash(python3 loop/controller.py *)
---

# /review — judge one packet's diff

Input: `$1`, a packet id at `status: review`.

Read `.claude/skills/review-criteria/SKILL.md` and
`.claude/skills/code-standards/SKILL.md` first. They are the checklist and
the rejection list; this file is only the procedure.

## Procedure

You are invoked by the controller when a packet reaches `status: review`,
which only happens after all six required CI checks are green. You can also
be invoked by hand for a second opinion.

**1. Get the diff — and only the diff.**

```bash
python3 loop/controller.py status | grep "$1"
git diff origin/main...<branch> --stat
git diff origin/main...<branch>
```

Do **not** read `loop/runs/`, the builder's `.loop/result.json` summary, or
its commit messages beyond the diff. The builder's rationale is a persuasion
artifact: it is written to make the diff look correct, by something better at
arguing than you are at resisting. Judge the artifact. If the diff needs the
argument to look right, it is wrong.

**2. Do not re-run CI, and do not re-check the acceptance tests by hand.**
The controller already proved both: every file in `loop/oracle.lock` was
re-hashed in the worktree before this PR could exist, and red CI becomes a
rework without ever reaching you. If you nonetheless see a pinned file in the
diff, stop and escalate — the guard failed, which is a larger finding than
anything else you might report.

**3. Work the checklist** in `review-criteria` §Checklist, A through F, in
order. Spend the time on §B (the ten invariants CI cannot grep) and §D (code
standards), and inside §B on *never guess*, which is the most common real
defect.

**4. Decide the verdict** — `approve`, `rework`, or `escalate`.

Before writing `rework`, ask once: *is the fix inside this packet's spec and
this packet's scope?* If not — the packet contradicts a PRD, its acceptance
tests cannot all pass together, its scope spans two branches, the right fix
means changing a shared pattern everywhere — then it is `escalate`, and
saying so now saves three builder runs discovering it the expensive way.

**5. Return the JSON verdict** through the structured-output channel requested
in your prompt, in the format `review-criteria` §Verdict format specifies.
You have no write-capable tool and must not change the checkout.

You do not set a status, you do not touch the packet file, and you do not
merge anything. The controller reads your JSON and decides. A malformed
verdict escalates the packet to the owner rather than defaulting to
anything, so get the shape right.

**6. If you approved while genuinely uncertain, populate
`judgement_calls`.** Those surface in the owner's digest under "approved, but
the reviewer called it a judgement call". An approval that hides a close call
is the exact failure this design exists to prevent. An honest one costs you
nothing.

## Output

One line per finding, most severe first, `file:line` and the spec clause.
No praise. No scope creep. Then the verdict and the new status.

The controller records the verdict in its append-only event ledger and audit
branch. If the owner cannot reconstruct your decision from that ledger plus
the reviewed git diff, you have not finished.
