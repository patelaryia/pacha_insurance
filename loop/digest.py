#!/usr/bin/env python3
"""The current loop digest — a standing document, not a log.

Four questions in the order the owner cares about them: what needs you, what
is stuck, what did the reviewer wave through while admitting it was a close
call, and what landed. Folded out of the ledger, so it cannot disagree with
the state it describes. Disposable — deleting it loses nothing.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import packets as P  # noqa: E402
import store  # noqa: E402

REPO = P.REPO
DIGEST = REPO / "loop" / "runs" / "digest.md"


def _titles(board_dir: pathlib.Path) -> dict:
    return {p.id: p for p in P.load_board(board_dir)}


def _judgement_calls(conn, packet_id: str) -> list[str]:
    row = conn.execute(
        "SELECT payload FROM events WHERE packet_id=? AND kind='review_verdict'"
        " ORDER BY seq DESC LIMIT 1", (packet_id,)).fetchone()
    return json.loads(row["payload"]).get("judgement_calls") or [] if row else []


def _merged_since(conn, since: float) -> list:
    return conn.execute(
        "SELECT DISTINCT packet_id, at FROM events WHERE to_status='merged' AND at>?"
        " ORDER BY at", (since,)).fetchall()


def render(conn, board_dir: pathlib.Path, config: dict) -> str:
    specs = _titles(board_dir)
    rows = store.all_packets(conn)
    by = lambda s: [r for r in rows if r["status"] == s]  # noqa: E731
    title = lambda i: specs[i].meta["title"] if i in specs else "(not on the board)"  # noqa: E731

    previous = _previous_generated()
    now = time.time()
    out = [
        "# Loop digest",
        "",
        f"generated: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))}",
        f"covering: since {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(previous))}",
        "",
    ]

    paused = REPO / "loop" / "PAUSED"
    if paused.exists():
        out += ["## THE LOOP IS PAUSED", "", f"`loop/PAUSED`: {paused.read_text().strip()}", ""]

    # --- waiting on you ------------------------------------------------------
    escalated, ready = by("escalated"), by("merge_ready")
    out += ["## Waiting on you", ""]
    if not escalated and not ready:
        out += ["Nothing.", ""]
    for row in escalated:
        out += [
            f"### {row['id']} — escalated",
            title(row["id"]),
            "",
            f"- **why:** {row['reason'] or 'no reason recorded'}",
            f"- attempts: {row['attempts']}, rework cycles: {row['rework_cycles']}",
        ]
        if row["pr_number"]:
            out.append(f"- PR: #{row['pr_number']}")
        out.append("")
    for row in ready:
        cmd = config["merge"]["merge_command"].replace("{pr}", str(row["pr_number"]))
        out += [
            f"### {row['id']} — approved, green, needs your merge",
            title(row["id"]),
            "",
            f"- PR: #{row['pr_number']}",
            "",
            "```bash",
            cmd,
            "```",
            "",
        ]

    # --- blocked -------------------------------------------------------------
    blocked = by("blocked")
    out += ["## Blocked", ""]
    out += ["Nothing.", ""] if not blocked else []
    for row in blocked:
        out.append(f"- **{row['id']}** ({row['attempts']} attempts) — "
                   f"{row['reason'] or 'no reason recorded'}")
    if blocked:
        out.append("")

    # --- judgement calls -----------------------------------------------------
    # The most useful section in the file: where the loop's judgement was
    # weakest and a human read is cheapest.
    out += ["## Approved, but the reviewer called it a judgement call", ""]
    any_calls = False
    for row in rows:
        calls = _judgement_calls(conn, row["id"])
        if not calls:
            continue
        any_calls = True
        out.append(f"### {row['id']} — {title(row['id'])} ({row['status']})")
        out += [f"- {call}" for call in calls]
        out.append("")
    if not any_calls:
        out += ["Nothing.", ""]

    # --- merged --------------------------------------------------------------
    merged = _merged_since(conn, previous)
    out += ["## Merged since you last looked", ""]
    out += ["Nothing.", ""] if not merged else []
    for row in merged:
        out.append(f"- **{row['packet_id']}** — {title(row['packet_id'])}")
    if merged:
        out.append("")

    # --- in flight -----------------------------------------------------------
    flight = by("building") + by("awaiting_ci") + by("review") + by("rework")
    out += ["## In flight", ""]
    out += ["Nothing.", ""] if not flight else []
    for row in flight:
        lease = " (leased)" if row["lease_owner"] else ""
        pr = f" PR#{row['pr_number']}" if row["pr_number"] else ""
        out.append(f"- {row['id']} — {row['status']}{pr}{lease} — {title(row['id'])}")
    if flight:
        out.append("")

    waiting = _waiting_on_deps(rows, specs)
    if waiting:
        out += ["## Queued, waiting on dependencies", ""]
        out += [f"- {pid} — waiting on {', '.join(deps)}" for pid, deps in sorted(waiting.items())]
        out.append("")

    spend = store.attempts_today(conn)
    tokens = f"{spend['tokens']:,}" if spend["tokens"] is not None else "not reported"
    out += [
        "## Budget today (UTC)",
        "",
        f"- builder time: {spend['builder_minutes']} / "
        f"{config['breakers']['daily_builder_minutes']} min",
        f"- attempts: {spend['attempts']} / {config['breakers']['daily_attempts']}",
        f"- tokens: {tokens}",
        "",
    ]
    return "\n".join(out) + "\n"


def _waiting_on_deps(rows, specs) -> dict:
    merged = {r["id"] for r in rows if r["status"] == "merged"}
    out = {}
    for row in rows:
        if row["status"] != "queued" or row["id"] not in specs:
            continue
        pending = [d for d in specs[row["id"]].meta["depends_on"] if d not in merged]
        if pending:
            out[row["id"]] = pending
    return out


def _previous_generated() -> float:
    if not DIGEST.exists():
        return 0.0
    for line in DIGEST.read_text().splitlines():
        if line.startswith("generated: "):
            return time.mktime(time.strptime(line.split(": ", 1)[1], "%Y-%m-%dT%H:%M:%SZ"))
    return 0.0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--board", default=None)
    parser.add_argument("--stdout", action="store_true")
    args = parser.parse_args(argv)
    config = P.load_config()
    with store.connect() as conn:
        store.init(conn)
        text = render(conn, REPO / (args.board or config["board_dir"]), config)
    if args.stdout:
        print(text)
    else:
        DIGEST.parent.mkdir(parents=True, exist_ok=True)
        DIGEST.write_text(text)
        print(f"wrote {DIGEST.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
