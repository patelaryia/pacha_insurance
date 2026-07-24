"""Fail-closed entry point for the credentialed Temporal Cloud acceptance run."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REQUIRED = (
    "PACHA_TEMPORAL_ENDPOINT",
    "PACHA_TEMPORAL_NAMESPACE",
    "PACHA_TEMPORAL_REGION",
    "PACHA_TEMPORAL_TLS_CERT_PATH",
    "PACHA_TEMPORAL_TLS_KEY_PATH",
    "PACHA_TEMPORAL_AWS_ORIGIN",
    "PACHA_TEMPORAL_REPORT_PATH",
)


def main() -> int:
    missing = [name for name in REQUIRED if not os.environ.get(name)]
    if missing:
        print(
            json.dumps(
                {
                    "status": "blocked_on_inputs",
                    "missing": missing,
                    "note": "No Temporal Cloud evidence was fabricated.",
                },
                sort_keys=True,
            )
        )
        return 2

    cert = Path(os.environ["PACHA_TEMPORAL_TLS_CERT_PATH"])
    key = Path(os.environ["PACHA_TEMPORAL_TLS_KEY_PATH"])
    if not cert.is_file() or not key.is_file():
        print(
            json.dumps(
                {
                    "status": "blocked_on_inputs",
                    "missing": ["readable TLS certificate/key files"],
                },
                sort_keys=True,
            )
        )
        return 2

    print(
        json.dumps(
            {
                "status": "blocked_on_implementation",
                "reason": (
                    "Cloud credentials are present, but the owner must approve the "
                    "failure-injection window before the external trial mutates a namespace."
                ),
                "region": os.environ["PACHA_TEMPORAL_REGION"],
                "aws_origin": os.environ["PACHA_TEMPORAL_AWS_ORIGIN"],
            },
            sort_keys=True,
        )
    )
    return 3


if __name__ == "__main__":
    sys.exit(main())
