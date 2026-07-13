"""Operational worker bootstrap for the document-intelligence pipeline."""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from claim_core import LocalBlobStore, create_app
from doc_intel.anthropic_client import AnthropicModelClient, build_anthropic_sdk_client
from doc_intel.engine import DocIntelEngine, build_engine


@dataclass(frozen=True)
class WorkerRuntime:
    app: Any
    engine: DocIntelEngine


class CeleryStageScheduler:
    def __init__(self, queue: str) -> None:
        if not queue:
            raise RuntimeError("doc-intel worker queue is not configured")
        self.queue = queue

    def schedule(self, document_id: str, stage: str) -> None:
        from doc_intel.tasks import PIPELINE_TASKS

        PIPELINE_TASKS[stage].apply_async(args=[document_id], queue=self.queue)


def _load_factory(reference: str) -> Any:
    module_name, separator, attribute = reference.partition(":")
    if not separator:
        raise RuntimeError("factory references must use module:attribute")
    return getattr(importlib.import_module(module_name), attribute)


def build_worker_runtime(
    *, sdk_client: Any | None = None, alert_sink: Any | None = None
) -> WorkerRuntime:
    """Build all worker dependencies from environment and pack configuration."""

    database_url = os.environ.get("DATABASE_URL")
    blob_root = os.environ.get("PACHA_BLOB_ROOT")
    if not database_url or not blob_root:
        raise RuntimeError("DATABASE_URL and PACHA_BLOB_ROOT are required for doc-intel workers")
    if alert_sink is None:
        factory_ref = os.environ.get("DOC_INTEL_ALERT_SINK_FACTORY")
        if not factory_ref:
            raise RuntimeError("DOC_INTEL_ALERT_SINK_FACTORY is required for doc-intel workers")
        alert_sink = _load_factory(factory_ref)()
    if sdk_client is None:
        sdk_client = build_anthropic_sdk_client()
    root = Path(__file__).resolve().parents[2]
    config = yaml.safe_load((root / "packs" / "motor" / "doc_intel.yaml").read_text())
    app = create_app(database_url, blob_store=LocalBlobStore(blob_root))
    model = AnthropicModelClient(sdk_client, config=config, ledger=app.state.claim_service)
    scheduler = CeleryStageScheduler(str(config["worker"]["queue"]))
    engine = build_engine(
        app,
        model_client=model,
        model_config=config,
        alert_sink=alert_sink,
        runtime_mode="worker",
        stage_scheduler=scheduler,
    )
    return WorkerRuntime(app=app, engine=engine)
