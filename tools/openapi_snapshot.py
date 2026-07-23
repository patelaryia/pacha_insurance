"""Generate the committed OpenAPI snapshots for the agent-owned HTTP surfaces.

Run from the repository root:

    python tools/openapi_snapshot.py            # rewrite the snapshots
    python tools/openapi_snapshot.py --check    # fail if a snapshot is stale

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
SNAPSHOT_DIR = REPO / "docs" / "openapi"
# PACKET-19 adds the review-scoped workspace and autosave routes, so the
# approval-pack snapshot covers both prefixes that surface now spans.
# PACKET-20 adds the five PRD-09 paste-assist routes under one prefix; PACKET-21
# adds the RPA panel, the authenticated evidence read, and the sampled paste
# readback capture. The internal runner contract is deliberately absent: it is
# mounted only when infra injects a runner machine authenticator.
SURFACES: dict[str, tuple[str, ...]] = {
    "approval_pack": (
        "/claims/{claim_id}/approval-pack",
        "/reviews/{review_id}/approval-note",
    ),
    "projection": (
        "/console/claims/{claim_id}/projections",
        "/console/reviews/{review_id}/paste-readback",
    ),
}

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


class _InertVerifier:
    """Never called by snapshot generation; no live tenant is contacted."""

    def verify(self, token: str) -> Any:
        raise RuntimeError("the OpenAPI snapshot never verifies a token")


def build_document() -> dict[str, Any]:
    """Build the whole application once and return its OpenAPI document."""

    from agent_runtime import build_agent_runtime
    from approval_pack_agent import build_approval_pack_agent
    from claim_core import create_app
    from cop_runtime import build_cop_runtime
    from eval_harness import build_eval_harness
    from projection_agent import build_projection_agent
    from review_queue import build_review_queue, install_console

    with tempfile.TemporaryDirectory(prefix="pacha-openapi-") as directory:
        app = create_app(f"sqlite:///{directory}/openapi.db")
        build_cop_runtime(app, pack_paths=[REPO / "packs" / "motor"])
        build_eval_harness(app, model_client=_InertModel())
        build_review_queue(app, roles={})
        build_agent_runtime(app)
        build_approval_pack_agent(
            app, model_client=_InertModel(), html_renderer=_InertRenderer()
        )
        install_console(app, verifier=_InertVerifier(), identities={}, roles={})
        build_projection_agent(app)
        return app.openapi()


def slice_paths(document: dict[str, Any], prefixes: tuple[str, ...]) -> dict[str, Any]:
    """Return one surface's slice of the generated document."""

    return {
        "openapi": document["openapi"],
        "info": document["info"],
        "paths": {
            path: operations
            for path, operations in sorted(document["paths"].items())
            if path.startswith(prefixes)
        },
    }


def render(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if a snapshot is stale")
    arguments = parser.parse_args()
    document = build_document()
    stale = False
    for name, prefixes in SURFACES.items():
        path = SNAPSHOT_DIR / f"{name}.json"
        generated = render(slice_paths(document, prefixes))
        if arguments.check:
            if not path.is_file() or path.read_text(encoding="utf-8") != generated:
                print(f"openapi snapshot {name} is stale: run python tools/openapi_snapshot.py")
                stale = True
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(generated, encoding="utf-8")
        print(f"wrote {path.relative_to(REPO)}")
    if arguments.check and not stale:
        print("openapi snapshots: OK")
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
