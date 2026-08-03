"""Owner-authorised T08 contract: remove the superseded runtime completely."""

from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]

DELETED_PRODUCTION_FILES = (
    "platform/claim_core/celery_app.py",
    "platform/doc_intel/tasks.py",
    "platform/eval_harness/tasks.py",
    "agents/projection_agent/tasks.py",
)

FORBIDDEN_RUNTIME_PATTERNS = (
    r"\bfrom celery\b",
    r"\bimport celery\b",
    r"\bcelery_app\b",
    r"\bCeleryStageScheduler\b",
    r"\bconfigure_reaper\b",
    r"\breap_stale_runs\b",
    r"\bredis://",
)


def _production_text() -> str:
    paths = (
        list((REPO / "platform").rglob("*.py"))
        + list((REPO / "agents").rglob("*.py"))
    )
    paths = [path for path in paths if "alembic/versions" not in path.as_posix()]
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


def test_t08_deletes_every_named_legacy_runtime_entry_point():
    for relative in DELETED_PRODUCTION_FILES:
        assert not (REPO / relative).exists(), relative

    production = _production_text()
    for pattern in FORBIDDEN_RUNTIME_PATTERNS:
        assert re.search(pattern, production, re.IGNORECASE) is None, pattern


def test_t08_removes_celery_redis_dependencies_and_ci_services():
    dependencies = (REPO / "requirements.txt").read_text(encoding="utf-8")
    assert re.search(r"(?mi)^\s*(celery|redis)(?:\[|[<>=~!]|$)", dependencies) is None

    workflows = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (REPO / ".github" / "workflows").glob("*.yml")
    )
    for pattern in (r"\bredis:\s*$", r"\bREDIS_URL\b", r"\bCELERY_BROKER_URL\b"):
        assert re.search(pattern, workflows, re.MULTILINE) is None, pattern


def test_t08_keeps_the_temporal_replacements_and_claim_read_boundary():
    required = (
        "platform/orchestration/workflows.py",
        "platform/orchestration/schedules.py",
        "platform/orchestration/starter.py",
        "platform/orchestration/worker.py",
    )
    for relative in required:
        assert (REPO / relative).is_file(), relative

    app_source = (REPO / "platform/claim_core/app.py").read_text(encoding="utf-8")
    assert "configure_runtime" not in app_source
    assert "build_temporal_client" not in app_source
