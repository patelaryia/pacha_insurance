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


class LocalBlobStore:
    """Filesystem-backed development/test blob store."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        candidate = (self.root / key).resolve()
        if self.root.resolve() not in candidate.parents:
            raise ValueError("blob key escapes the configured root")
        return candidate

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()
