"""Public interface for the PRD-01 document-intelligence substrate."""

from typing import Any

__all__ = [
    "AnthropicModelClient",
    "DocIntelEngine",
    "PipelineOutcome",
    "StageResult",
    "build_engine",
    "build_worker_runtime",
]


def __getattr__(name: str) -> Any:
    """Keep pure validators importable without loading document-rendering adapters."""

    if name in {"DocIntelEngine", "build_engine"}:
        from doc_intel.engine import DocIntelEngine, build_engine

        return {"DocIntelEngine": DocIntelEngine, "build_engine": build_engine}[name]
    if name in {"PipelineOutcome", "StageResult"}:
        from doc_intel.stages import PipelineOutcome, StageResult

        return {"PipelineOutcome": PipelineOutcome, "StageResult": StageResult}[name]
    if name == "AnthropicModelClient":
        from doc_intel.anthropic_client import AnthropicModelClient

        return AnthropicModelClient
    if name == "build_worker_runtime":
        from doc_intel.runtime import build_worker_runtime

        return build_worker_runtime
    raise AttributeError(name)
