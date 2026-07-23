"""PACKET-21 acceptance — the outbound-only runner contract (§18).

Protected (CODEOWNERS): the builder may not weaken this file once merged.

Two halves:

* the *image* contract, asserted against the committed Dockerfile, entrypoint,
  and deployment target registry — no listener, non-root, read-only root
  filesystem, pinned Python/Playwright/browser, and no baked secret or selector;
* the *execution* contract, asserted against the exact executor driving the
  deterministic synthetic target — exact selector use, credential frames
  excluded, values kept out of runner logs, and the browser context always
  closed.

No production target is contacted and no container is published: `entrypoint.py`
exits `78` with its structured blocker list, which is the honest state until
PACKET-22 supplies the machine identity and the captured paths.
"""
from __future__ import annotations

import ast
import json
import pathlib
import re
import subprocess
import sys

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
RUNNER = REPO / "infra" / "rpa_runner"
DOCKERFILE = (RUNNER / "Dockerfile").read_text(encoding="utf-8")
ENTRYPOINT = (RUNNER / "entrypoint.py").read_text(encoding="utf-8")
#: Only the actual build instructions; the contract comment names what is banned.
INSTRUCTIONS = "\n".join(
    line for line in DOCKERFILE.splitlines() if not line.lstrip().startswith("#")
)


# --- 1. no inbound surface, non-root ---------------------------------------------------


def test_the_image_exposes_no_port_and_runs_as_a_non_root_user():
    for forbidden in (
        "EXPOSE",
        "--remote-debugging-port",
        "HEALTHCHECK",
        "uvicorn",
        "gunicorn",
        "--host 0.0.0.0",
        "LISTEN",
    ):
        assert forbidden not in INSTRUCTIONS, forbidden
    assert re.search(r"^USER runner$", DOCKERFILE, re.MULTILINE)
    assert "useradd --system" in DOCKERFILE
    assert "--uid 10001" in DOCKERFILE
    # The entrypoint is a client: it imports no server or socket machinery. The
    # check reads the parsed module, so a docstring naming what it does not do
    # cannot fail it and a real import cannot hide behind a comment.
    imported: set[str] = set()
    for node in ast.walk(ast.parse(ENTRYPOINT)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"socket", "socketserver", "http", "fastapi", "flask", "uvicorn"}
    assert not re.search(r"\b(bind|listen|serve_forever)\s*\(", ENTRYPOINT)


# --- 2. the outbound allowlist ---------------------------------------------------------


def test_the_outbound_allowlist_is_exactly_the_four_declared_destinations():
    namespace: dict[str, object] = {
        "__file__": str(RUNNER / "entrypoint.py"),
        "__name__": "runner_entrypoint",
    }
    exec(compile(ENTRYPOINT, "entrypoint.py", "exec"), namespace)  # noqa: S102
    assert namespace["ALLOWED_OUTBOUND"] == (
        "platform_control_api",
        "target_system",
        "aws_secrets_manager",
        "evidence_upload",
    )


# --- 3. pinned versions and a deterministic install ------------------------------------


def test_python_playwright_and_the_browser_revision_are_pinned():
    base = r"^FROM python:3\.12\.\d+-slim-bookworm@sha256:[0-9a-f]{64}"
    assert re.search(base, DOCKERFILE, re.MULTILINE)
    assert re.search(r"ARG PLAYWRIGHT_VERSION=\d+\.\d+\.\d+", DOCKERFILE)
    assert re.search(r"ARG PLAYWRIGHT_BROWSER_REVISION=\d+", DOCKERFILE)
    assert 'playwright==${PLAYWRIGHT_VERSION}' in DOCKERFILE
    # No floating tag and no unpinned dependency.
    for line in DOCKERFILE.splitlines():
        if line.strip().startswith('"') and "==" not in line and line.strip().endswith('" \\'):
            pytest.fail(f"unpinned dependency: {line}")
    assert ":latest" not in DOCKERFILE


# --- 4. no secret and no production selector in the image ------------------------------


def test_no_secret_or_production_selector_is_copied_into_the_image():
    copied = re.findall(r"^COPY [^\n]*?([\w./-]+) /app/", DOCKERFILE, re.MULTILINE)
    assert set(copied) == {"entrypoint.py", "targets.yaml"}
    registry = yaml.safe_load((RUNNER / "targets.yaml").read_text(encoding="utf-8"))
    for record in registry["systems"].values():
        assert record["status"] == "blocked_on_inputs"
        assert record["base_url"] is None
        assert record["secret_ref"] is None
    body = DOCKERFILE + ENTRYPOINT + json.dumps(registry)
    for forbidden in ("PRIVATE KEY", "client_secret", "#claimNo", "role=button"):
        assert forbidden.lower() not in body.lower(), forbidden


# --- 5. read-only root filesystem and ephemeral browser storage ------------------------


def test_the_only_writable_paths_are_ephemeral_browser_and_tmp_scratch():
    volumes = re.findall(r"^VOLUME \[([^\]]+)\]", DOCKERFILE, re.MULTILINE)
    assert volumes, "the ephemeral mounts must be declared"
    declared = {value.strip().strip('"') for value in volumes[0].split(",")}
    assert declared == {"/var/run/pacha-browser", "/tmp/pacha"}
    assert "read-only root filesystem" in (RUNNER / "README.md").read_text(encoding="utf-8")


def test_the_entrypoint_refuses_to_start_and_reports_every_blocker():
    result = subprocess.run(  # noqa: S603 - fixed repository-local command
        [sys.executable, str(RUNNER / "entrypoint.py")],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": ""},
    )
    assert result.returncode == 78, result.stderr
    body = json.loads(result.stdout)
    assert body["status"] == "blocked_on_inputs"
    blockers = {row["blocker"] for row in body["blockers"]}
    assert {"runner-machine-identity", "control-api-url", "target-icon", "target-edms"} <= blockers


# --- 6. exact selector use -------------------------------------------------------------


def _executor(target, click_path, **kwargs):
    from projection_agent.config import parse_click_path
    from projection_agent.runner.browser import ExactExecutor

    parsed = parse_click_path(
        click_path,
        operation_id=click_path["operation"],
        version=click_path["version"],
        executable=True,
    )
    return ExactExecutor(target.session(), parsed, **kwargs), parsed


class _Timeouts:
    def step_timeout(self, timeout_class: str) -> int:
        return {"default": 20, "edms": 90, "upload": 480}[timeout_class]


@pytest.fixture()
def workflow():
    from fixtures.projection import EDMS_CLAIMS_WORKFLOW

    return json.loads(json.dumps(EDMS_CLAIMS_WORKFLOW))


@pytest.fixture()
def target():
    from fixtures.projection import SyntheticTarget

    return SyntheticTarget(reference="EDMS/2026/004521")


def _values(click_path) -> dict[str, str]:
    return {step.id: f"value-{step.id}" for step in click_path.steps if step.value_kind}


def test_zero_or_multiple_matches_halt_and_the_runner_never_hunts(workflow, target):
    from projection_agent.runner.browser import SelectorDrift

    executor, parsed = _executor(target, workflow, timeouts=_Timeouts())
    target.rename("#insuredPin")
    with pytest.raises(SelectorDrift) as drift:
        executor.run(_values(parsed))
    assert drift.value.reason_code == "selector_no_match"
    assert drift.value.step_id == "s5"
    # It halted: no later selector was touched and nothing was submitted.
    assert "#insuredDl" not in target.values
    assert target.submitted is False

    executor, parsed = _executor(target, workflow, timeouts=_Timeouts())
    target.cardinality.clear()
    target.duplicate("#lossDate")
    with pytest.raises(SelectorDrift) as duplicate:
        executor.run(_values(parsed))
    assert duplicate.value.reason_code == "selector_multiple_matches"


# --- 7/8. credentials and values never leave the runner --------------------------------


def test_credential_controls_are_excluded_from_screenshots(target):
    from fixtures.projection.target import CREDENTIAL_SELECTORS

    assert target.forbidden_frames == CREDENTIAL_SELECTORS
    session = target.session()
    frame = session.screenshot()
    assert b"password" not in frame.lower()
    assert b"username" not in frame.lower()
    session.close()


def test_target_values_and_cookies_do_not_enter_runner_logs(workflow, target):
    executor, parsed = _executor(target, workflow, timeouts=_Timeouts())
    values = _values(parsed)
    executor.run(values)
    log = "\n".join(executor.session.log)  # type: ignore[attr-defined]
    for value in values.values():
        assert value not in log
    assert "cookie" not in log.lower()
    # Only ids and actions are recorded.
    assert log.splitlines()[0].startswith("fill #")


# --- 9. the context always closes ------------------------------------------------------


def test_the_browser_context_closes_after_success_failure_and_cancellation(target, workflow):
    from fixtures.projection import build_fixture_adapter
    from fixtures.projection.runner import register_definition
    from projection_agent.runner.browser import SelectorDrift

    def adapter():
        built = build_fixture_adapter(
            "edms",
            target=target,
            timeouts=_Timeouts(),
            clock=lambda: __import__("datetime").datetime.now(__import__("datetime").UTC),
        )
        register_definition(built, workflow)
        return built

    from projection_agent.config import parse_click_path

    parsed = parse_click_path(
        workflow, operation_id=workflow["operation"], version=workflow["version"], executable=True
    )
    payload = {"values": _values(parsed)}

    assert adapter().execute("edms.claims_workflow", payload, "run-1").outcome == "submitted"
    assert target.closed_sessions == 1

    target.rename("#insuredDl")
    assert adapter().execute("edms.claims_workflow", payload, "run-2").outcome == "ui_drift"
    assert target.closed_sessions == 2

    target.cardinality.clear()
    target.raise_signature = "EDMS-DUP-FILENAME"
    assert adapter().execute("edms.claims_workflow", payload, "run-3").outcome == "known_failure"
    assert target.closed_sessions == 3

    # A callback failure inside the frame hook is still an exception mid-run.
    def exploding(_frame):
        raise RuntimeError("evidence upload failed")

    built = build_fixture_adapter(
        "edms",
        target=target,
        timeouts=_Timeouts(),
        clock=lambda: __import__("datetime").datetime.now(__import__("datetime").UTC),
        on_frame=exploding,
    )
    register_definition(built, workflow)
    assert built.execute("edms.claims_workflow", payload, "run-4").outcome == "uncertain_write"
    assert target.closed_sessions == 4
    assert target.open_sessions == target.closed_sessions
    assert SelectorDrift("s1", "x").reason_code == "x"
