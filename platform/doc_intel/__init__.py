"""Public interface for the PRD-01 document-intelligence substrate."""

from typing import Any

__all__ = [
    "AnthropicModelClient",
    "DocIntelEngine",
    "PipelineOutcome",
    "StageResult",
    "build_engine",
    "build_worker_runtime",
    "registered_doc_types",
]


def registered_doc_types(pack_id: str = "motor") -> tuple[str, ...]:
    """Return the PRD-01 document-type registry without building the engine."""

    from pathlib import Path

    import yaml

    directory = Path(__file__).with_name("schemas") / pack_id
    if not directory.is_dir():
        raise LookupError(f"unknown document schema pack {pack_id!r}")
    doc_types: set[str] = set()
    for path in sorted(directory.glob("*.yaml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        doc_type = payload.get("doc_type") if isinstance(payload, dict) else None
        if not isinstance(doc_type, str) or not doc_type:
            raise ValueError(f"document schema {path.name} has no doc_type")
        doc_types.add(doc_type)
    return tuple(sorted(doc_types))


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
