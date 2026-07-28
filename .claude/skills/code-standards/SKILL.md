---
name: code-standards
description: Use when writing or reviewing any code in the Pacha repository - the binding standard for defensiveness, complexity, locality and money handling, and the list the reviewer rejects on
---

# Code standards

Both agents read this every run. The builder writes to it. The reviewer
**rejects** on it.

That word is literal. A violation of anything marked **REJECT** below is a
blocking finding. The reviewer does not note it, does not suggest it as a
follow-up, and does not approve with a comment. It sends the packet back
with the rule cited. A standard the reviewer merely mentions is not a
standard, it is a preference, and preferences do not survive a loop.

## Why this file exists

Current models default to code that is defensive, over-complex and reasoned
about locally. Every one of those defaults looks responsible in isolation. A
null check looks careful. A fallback default looks robust. A strategy object
looks extensible.

A loop amplifies this. Each iteration adds one more small defence to satisfy
one more failing case, and every addition is individually defensible. After
eleven packets the system is measurably harder to understand and no more
correct, and nobody can point at the commit where it went wrong — because
there isn't one. The rules below exist to make each of those individually
defensible additions **rejectable in isolation**, before it accumulates.

Pacha is pre-pilot and heading into a regulated insurance environment. A
crash today is free and informative. A swallowed error today is a wrong
settlement amount in production later, with no stack trace to find it by.

---

## 1. Defensiveness

### 1.1 Make invalid states unrepresentable rather than handling them

**REJECT:** a function that validates its own input where a type or
constructor could have made the invalid input impossible.

Before writing a check, ask: could the caller have been unable to pass this?
If the answer is yes, fix the type, not the function.

```python
# REJECT — the check exists because the type permits the bad state
def payable(claim: dict) -> Money:
    if "excess_cents" not in claim:
        raise ValueError("missing excess")
    if claim["excess_cents"] < 0:
        raise ValueError("negative excess")
    ...

# ACCEPT — the bad state cannot be constructed, so there is nothing to check
class Excess(NamedTuple):
    cents: int
    def __post_init__(self) -> None:
        assert self.cents >= 0, "excess is a non-negative amount by construction"

def payable(claim: PricedClaim) -> Money:
    ...
```

This repo already works this way — `Money` is a distinct type precisely so
that ED-8 is a type error rather than a review comment. Follow that.

### 1.2 Do not catch an exception you cannot meaningfully handle

**REJECT:** `except Exception`, bare `except:`, and any catch whose handler
logs and continues.

"Meaningfully handle" means the handler restores a correct state or converts
the failure into a **visible** outcome the spec names — an `EXCEPTION`
review item, `blocked_on_inputs`, a 409, a refused render. It does not mean
"stop the traceback reaching the user".

```python
# REJECT — the caller now proceeds with a claim it does not have
try:
    claim = load_claim(claim_id)
except Exception as exc:
    logger.warning("could not load claim: %s", exc)
    claim = None

# ACCEPT — let it crash; a missing claim here is a bug, not a condition
claim = load_claim(claim_id)

# ACCEPT — the failure becomes a named, visible outcome from the spec
try:
    result = adapter_readback(op)
except ReadbackTimeout:
    raise UncertainWrite(subtype="uncertain_write", op_id=op.id) from None
```

A stack trace is information. A caught-and-logged exception is a bug that
ships, because nothing downstream knows the value it is holding is wrong.

### 1.3 No fallback defaults that mask upstream failure

**REJECT:** `?? 0`, `or {}`, `or []`, `.get(k, <default>)` on a key that is
required, `except: pass`, `getattr(x, k, None)` used as flow control, and
any catch-log-continue.

The single exception: the fallback is a **deliberate product decision**, and
a comment on the line says what the decision is and who made it. "Defensive"
is not a product decision.

```python
# REJECT — a missing reserve silently becomes a zero settlement
amount = claim.get("reserve_total_cents", 0)

# REJECT — hides which of two upstream stages failed
config = load_pack_config() or {}

# ACCEPT — commented, deliberate, and the default is the spec's answer
# PRD-08 §8.6: an absent savings ledger means no savings were recorded,
# which is a real zero, not a missing value. Owner-confirmed 2026-07-24.
savings_cents = ledger.total_cents if ledger else 0
```

Note what makes the third one acceptable: the zero is *semantically correct*,
not merely *convenient*. If you cannot write that sentence about your
default, you do not have a product decision, you have a mask.

### 1.4 Validate once at the trust boundary, then trust the types

**REJECT:** the same invariant re-checked in a second function inside the
boundary.

Boundaries in this repo are: HTTP request handlers (Pydantic), pack config
load, adapter responses, and anything crossing an `execute_or_stage` call.
Inside those, a `Money` is a `Money`. Re-asserting it isn't is not caution,
it is evidence that the contract is unclear — and the fix is the contract,
not a third check.

If you find yourself wanting to re-validate, the honest options are: tighten
the type, move the boundary, or escalate that the contract is wrong. Adding
the check is not one of them.

### 1.5 Fail early and loudly. Never degrade silently

**REJECT:** any path where a missing input, an ambiguous match, or a failed
external call results in the system continuing with a plausible-looking
value.

This is the invariant AGENTS.md §3.5 already calls *never guess*, and it is
the single most common thing the reviewer catches. Every ambiguous branch
resolves to a named, visible state — `EXCEPTION`, `blocked_on_inputs`, a
review item, a refused render — or it is rejected. "Picks the first match"
and "falls back to the previous version" are guesses wearing a jacket.

---

## 2. Complexity

### 2.1 No abstraction before the third concrete use

**REJECT:** a base class, generic helper, mixin, or shared module introduced
to serve two callers.

Two similar things are two things. They will diverge, and the abstraction
will then grow a flag to hold them together — which is how the flag in 2.2
gets born. Wait for the third. The duplication is cheaper than the wrong
seam, and it is visible, which the wrong seam is not.

### 2.2 No configuration flags, strategy objects or extension points with one caller

**REJECT:** a parameter that is only ever passed one value; a strategy or
handler interface with one implementation; a hook nothing hooks; a
`mode=`/`kind=`/`strategy=` argument introduced "for later".

Later is not a caller. Delete the parameter and inline the one behaviour.

This does **not** apply to values the spec says belong in the pack — model
ids, budgets, thresholds, SLAs, templates, click-paths and rules are data by
AGENTS.md §4 even when there is one pack today. Config-over-code is a spec
requirement; speculative flags are not the same thing, and the difference is
whether the PRD names the value.

### 2.3 Prefer deleting code to adding a branch

When a test fails, the first question is what to remove. A new `if` is the
last resort, not the first. If the new branch is genuinely required, the
reviewer will ask which spec clause requires it — have an answer.

### 2.4 A function that needs a comment to explain *what* it does is too big

Comments explain **why**: the spec clause, the non-obvious constraint, the
decision and who made it. This file and this repo are full of that kind of
comment and they are correct.

A comment that narrates *what* the next block does is a function name that
was never extracted. **REJECT** the block, not the comment.

---

## 3. Locality

### 3.1 Reason about the invariant, not the symptom

**REJECT:** a null check, a guard clause, or a defensive coercion added in
response to a failing test, without an explanation of why the value could be
null in the first place.

This is the rule that matters most in a loop, because fixing the symptom
always makes the test pass and always leaves the cause in place for the next
packet to trip over.

When a test fails because a value was null:

1. Find out where it became null.
2. Fix it there, or establish that null is legitimate at that point.
3. If null is legitimate, the type should say so, and the handling belongs
   where the meaning is known — not where the crash happened.

"Added a null check to fix the failing test" is a rejection every time,
regardless of whether the test now passes.

### 3.2 Match how the codebase already solves this problem

**REJECT:** a second pattern for a problem the repo already solves.

Before introducing an approach, find the existing one. This repo has settled
answers for append-only writes, idempotency keys, the single-writer ledger,
`execute_or_stage`, ULIDs, StrictUndefined rendering, and error shapes. Use
them.

If you believe the existing pattern is wrong, you have exactly two options:

- follow it anyway, or
- **escalate** to change it everywhere.

Never both. A codebase with two answers to one question is worse than a
codebase with the wrong answer, because now every reader has to work out
which one applies here.

---

## 4. Domain

### 4.1 Money is never a float

Integer minor units (KES cents, `BIGINT`) or `Decimal`. Never `float`,
never `round()` on a float, never a float literal in a money path. Literals
use the repo's `_00` cents convention: `4_000_000_00`.

CI greps the obvious form of this (`tools/ci/money_float_lint.py`, ED-8).
The reviewer catches the rest: a float that becomes money two calls later, a
ratio applied before conversion to cents, a JSON round-trip through a
JavaScript number.

### 4.2 Amounts payable are pure, boundary-tested and side-effect free

Any function computing an amount payable must:

- take values and return a value — no I/O, no clock, no database, no
  logging, no mutation of its arguments;
- be unit-tested **at the boundary values**, not near them. The spec's
  boundaries are exact and the tests must be exact: estimate = excess
  exactly; quote = 50.0% PAV exactly; R-05 strictly greater; band bound
  `100_000_00` inclusive; 50,000 desk / 50,001 physical; `4_000_000_00` → MD
  and one cent above → chairman.

`packs/*/calcs.py` is the 100%-coverage tier. A test that exercises 49.9%
and 50.1% but not 50.0% does not discharge the boundary requirement, and the
reviewer rejects it.

---

## 5. What the reviewer does with this file

Every rule above marked **REJECT** produces a blocking finding, cited as
`code-standards §N.N`, with `file:line`. Blocking means the packet returns
as `rework` and does not merge.

The reviewer does not accept these arguments:

| Argument | Answer |
|---|---|
| "It's defensive, it can't hurt" | §1.3. It hides which stage failed. |
| "The test passes now" | §3.1. The test passing is not the claim under review. |
| "We'll need the flag later" | §2.2. Later is not a caller. |
| "It's only a small check" | §1.4. Its size is not the problem, its existence is. |
| "This is how I'd normally do it" | §3.2. Match this codebase, or escalate. |
| "The spec is ambiguous here so I picked X" | §1.5 and AGENTS.md §5. Never guess. |

If the builder disagrees with a rule in this file, the route is escalation
with a written argument — not a local exception, and not a comment saying
the rule was considered.
