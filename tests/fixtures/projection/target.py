"""A deterministic synthetic target served inside the test process.

It is not a browser and not a stub for one: it is a tiny form model that
implements exactly the `BrowserSession` surface the exact executor needs, so
selector cardinality, postconditions, screenshots, and context closure can all
be asserted without Playwright, a network, or a real system.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from projection_agent.runner.browser import BrowserSession, TargetKnownFailure

CREDENTIAL_SELECTORS = frozenset({"#username", "#password"})


@dataclass
class SyntheticTarget:
    """One target system's durable state across sessions."""

    reference: str
    #: selector -> number of matching elements. A renamed selector drops to zero.
    cardinality: dict[str, int] = field(default_factory=dict)
    values: dict[str, str] = field(default_factory=dict)
    submitted: bool = False
    #: Stored records, keyed by the probe's captured deterministic key.
    records: list[dict[str, Any]] = field(default_factory=list)
    #: Set to a captured signature to make the next write raise it.
    raise_signature: str | None = None
    #: Set true to make the reference never appear (slow EDMS reflection).
    reflect: bool = True
    logged_in: bool = True
    module: str = "Claims Workflow"
    open_sessions: int = 0
    closed_sessions: int = 0
    screenshots: int = 0
    #: Selectors the target refuses to photograph. Credentials are never framed.
    forbidden_frames: frozenset[str] = CREDENTIAL_SELECTORS

    def rename(self, selector: str) -> None:
        """Simulate a UI change: the captured selector no longer resolves."""

        self.cardinality[selector] = 0

    def duplicate(self, selector: str) -> None:
        self.cardinality[selector] = 2

    def session(self) -> SyntheticSession:
        self.open_sessions += 1
        return SyntheticSession(self)


class SyntheticSession(BrowserSession):
    """One isolated context. Reused after `close()` is a test failure."""

    def __init__(self, target: SyntheticTarget) -> None:
        self.target = target
        self.closed = False
        self.log: list[str] = []

    # -- lifecycle -------------------------------------------------------------

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.target.closed_sessions += 1

    def _live(self) -> None:
        if self.closed:
            raise RuntimeError("a closed browser context was reused")

    # -- surface ---------------------------------------------------------------

    def count(self, selector: str) -> int:
        self._live()
        return self.target.cardinality.get(selector, 1)

    def fill(self, selector: str, value: str, *, timeout_seconds: int) -> None:
        self._live()
        del timeout_seconds
        # Values are recorded, never logged: a runner log must not carry them.
        self.log.append(f"fill {selector}")
        self.target.values[selector] = value

    def select(self, selector: str, value: str, *, timeout_seconds: int) -> None:
        self.fill(selector, value, timeout_seconds=timeout_seconds)

    def click(self, selector: str, *, timeout_seconds: int) -> None:
        self._live()
        del timeout_seconds
        self.log.append(f"click {selector}")
        if self.target.raise_signature is not None:
            signature = self.target.raise_signature
            self.target.raise_signature = None
            raise TargetKnownFailure("s15", signature)
        self.target.submitted = True
        self.target.records.append(dict(self.target.values))

    def value_of(self, selector: str) -> str | None:
        self._live()
        return self.target.values.get(selector)

    def text_of(self, selector: str) -> str | None:
        self._live()
        if selector == "#workflowReference":
            if not self.target.submitted or not self.target.reflect:
                return None
            return self.target.reference
        return self.target.values.get(selector)

    def visible(self, selector: str) -> bool:
        self._live()
        if selector == "#workflowReference":
            return self.target.submitted and self.target.reflect
        return self.count(selector) == 1

    def screenshot(self) -> bytes:
        self._live()
        self.target.screenshots += 1
        # Deterministic bytes so the manifest digest is reproducible. A
        # credential field is never part of the captured frame.
        material = f"{self.target.reference}:{self.target.screenshots}".encode()
        return b"PNG:" + hashlib.sha256(material).hexdigest().encode("ascii")

    def assert_precondition(self, assertion: str, equals: str | None) -> bool:
        self._live()
        if assertion == "logged_in":
            return self.target.logged_in
        if assertion == "module":
            return self.target.module == equals
        return False


__all__ = ["CREDENTIAL_SELECTORS", "SyntheticSession", "SyntheticTarget"]
