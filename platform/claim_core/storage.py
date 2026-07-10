"""Blob storage boundary for documents and immutable audit anchors."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class BlobStore(Protocol):
    """Minimal object-storage interface carried into the infrastructure packet."""

    def put(self, key: str, data: bytes) -> None:
        """Store immutable bytes at ``key``."""

    def get(self, key: str) -> bytes:
        """Read bytes previously stored at ``key``."""

    def exists(self, key: str) -> bool:
        """Return whether ``key`` is present."""

    def list_keys(self, prefix: str) -> list[str]:
        """Return stored keys beneath ``prefix`` in deterministic order."""


class LocalBlobStore:
    """Filesystem-backed development/test blob store."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        root = self.root.resolve()
        candidate = (root / key).resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError("blob key escapes the configured root")
        return candidate

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def list_keys(self, prefix: str) -> list[str]:
        base = self._path(prefix)
        if base.is_file():
            return [prefix]
        if not base.exists():
            return []
        root = self.root.resolve()
        return sorted(
            path.relative_to(root).as_posix()
            for path in base.rglob("*")
            if path.is_file()
        )
