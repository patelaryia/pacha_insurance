"""Generate the committed OpenAPI snapshot for the PRD-08 approval-pack surface.

Run from the repository root:

    python tools/openapi_snapshot.py            # rewrite the snapshot
    python tools/openapi_snapshot.py --check    # fail if the snapshot is stale

The application is built with inert local stubs: no model provider, browser, or
object store is contacted.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
SNAPSHOT = REPO / "docs" / "openapi" / "approval_pack.json"
PREFIX = "/claims/{claim_id}/approval-pack"

for package in ("platform", "agents", "packs"):
    sys.path.insert(0, str(REPO / package))


class _InertRenderer:
    """Never called by snapshot generation; present only to satisfy the builder."""

    def render(self, html: str, *, policy: Any) -> Any:
        raise RuntimeError("the OpenAPI snapshot never renders")


class _InertModel:
    """Never called by snapshot generation; present only to satisfy the builder."""

    def structured_call(self, *, tier: str, schema: dict, inputs: dict) -> dict:
        raise RuntimeError("the OpenAPI snapshot never calls a model")


def build_snapshot() -> dict[str, Any]:
    """Return the approval-pack slice of the generated OpenAPI document."""

    from agent_runtime import build_agent_runtime
    from approval_pack_agent import build_approval_pack_agent
    from claim_core import create_app
    from cop_runtime import build_cop_runtime
    from eval_harness import build_eval_harness
    from review_queue import build_review_queue

    with tempfile.TemporaryDirectory(prefix="pacha-openapi-") as directory:
        app = create_app(f"sqlite:///{directory}/openapi.db")
        build_cop_runtime(app, pack_paths=[REPO / "packs" / "motor"])
        build_eval_harness(app, model_client=_InertModel())
        build_review_queue(app, roles={})
        build_agent_runtime(app)
        build_approval_pack_agent(
            app, model_client=_InertModel(), html_renderer=_InertRenderer()
        )
        document = app.openapi()
    paths = {
        path: operations
        for path, operations in sorted(document["paths"].items())
        if path.startswith(PREFIX)
    }
    return {
        "openapi": document["openapi"],
        "info": document["info"],
        "paths": paths,
    }


def render(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the snapshot is stale")
    arguments = parser.parse_args()
    generated = render(build_snapshot())
    if arguments.check:
        if not SNAPSHOT.is_file() or SNAPSHOT.read_text(encoding="utf-8") != generated:
            print("openapi snapshot is stale: run python tools/openapi_snapshot.py")
            return 1
        print("openapi snapshot: OK")
        return 0
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(generated, encoding="utf-8")
    print(f"wrote {SNAPSHOT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
