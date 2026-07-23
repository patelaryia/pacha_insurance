"""Runner container entrypoint (PACKET-21 §7).

This process is a client and nothing else. It binds no socket, starts no
server, and exits non-zero rather than degrading to an unauthenticated or
unpinned path.

It refuses to start unless:

* a target record for every declared system is `live` in `targets.yaml`;
* the platform control API base URL is HTTPS;
* a runner machine identity is configured.

None of those hold today, so the container reports its blockers and exits. That
is the honest state: PACKET-21 builds the runtime, PACKET-22 activates it.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

#: The exact outbound allowlist. Anything else is a configuration defect.
ALLOWED_OUTBOUND = (
    "platform_control_api",
    "target_system",
    "aws_secrets_manager",
    "evidence_upload",
)
TARGETS = Path(__file__).resolve().parent / "targets.yaml"


def blockers() -> list[dict[str, str]]:
    """Every reason this runner may not start, as structured data."""

    import yaml

    found: list[dict[str, str]] = []
    control_api = os.environ.get("PACHA_CONTROL_API_URL", "")
    if not control_api.startswith("https://"):
        found.append(
            {"blocker": "control-api-url", "detail": "an HTTPS control API URL is required"}
        )
    if not os.environ.get("PACHA_RUNNER_IDENTITY"):
        found.append(
            {
                "blocker": "runner-machine-identity",
                "detail": "infra supplies the machine identity in PACKET-22",
            }
        )
    registry = yaml.safe_load(TARGETS.read_text(encoding="utf-8"))
    for system, record in (registry.get("systems") or {}).items():
        if record.get("status") != "live":
            found.append(
                {
                    "blocker": f"target-{system}",
                    "detail": str(record.get("blocked_on") or "blocked_on_inputs"),
                }
            )
    return found


def main() -> int:
    reasons = blockers()
    if reasons:
        # Structured, machine-readable, and free of any secret or selector.
        print(
            json.dumps(
                {
                    "status": "blocked_on_inputs",
                    "role": os.environ.get("PACHA_RUNNER_ROLE", "projection-runner"),
                    "outbound_allowlist": list(ALLOWED_OUTBOUND),
                    "blockers": reasons,
                },
                sort_keys=True,
            )
        )
        return 78  # EX_CONFIG
    raise SystemExit(  # pragma: no cover - unreachable until PACKET-22
        "a live runner loop is PACKET-22 work"
    )


if __name__ == "__main__":
    sys.exit(main())
