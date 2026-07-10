"""Machine-readable API errors."""


from __future__ import annotations

from typing import Any


class ClaimCoreError(Exception):
    """An expected Packet-1 API error."""

    def __init__(
        self,
        status_code: int,
        code: str,
        detail: str,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.extra = extra or {}


class HumanOverrideProtected(ClaimCoreError):
    """A write attempted to supersede a human-verified field."""

    def __init__(self, path: str) -> None:
        super().__init__(
            409,
            "HUMAN_OVERRIDE_PROTECTED",
            f"Human-verified field {path!r} cannot be superseded",
        )
        self.path = path
