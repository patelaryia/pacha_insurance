"""Exact click-path execution in one isolated browser context (PACKET-21 §9).

The rules this module exists to enforce:

* a captured selector is used exactly once; zero or several matches is
  `ui_drift` and the run halts. The executor never hunts for an alternative,
  never infers a neighbouring element, and never continues past a miss;
* every executed step is bracketed by a **before** and an **after** screenshot,
  and a failed selector or postcondition captures a failure frame when the
  browser can still take one;
* credential and login fields are never screenshotted;
* the context is created per run and always closed in ``finally``.

The concrete Playwright session lives behind :class:`BrowserSession` so the
control-plane acceptance suite can drive a deterministic synthetic target in the
test process. The production session is constructed in the runner container.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from projection_agent.config import ClickPath, Step

#: Screens the runner must never photograph.
CREDENTIAL_SCREEN = "authentication"


class SelectorDrift(Exception):
    """A selector resolved to zero or several elements, or an assertion failed."""

    def __init__(self, step_id: str | None, reason_code: str) -> None:
        self.step_id = step_id
        self.reason_code = reason_code
        super().__init__(f"{step_id}: {reason_code}")


class TargetKnownFailure(Exception):
    """The target returned a *captured* failure signature."""

    def __init__(self, step_id: str, signature: str) -> None:
        self.step_id = step_id
        self.signature = signature
        super().__init__(signature)


class ReflectionTimeout(Exception):
    """A known-slow EDMS write did not become visible within its pack budget."""

    def __init__(self, step_id: str) -> None:
        self.step_id = step_id
        super().__init__(step_id)


class BrowserSession(Protocol):
    """The minimal exact-execution surface one isolated context must provide."""

    def count(self, selector: str) -> int: ...

    def fill(self, selector: str, value: str, *, timeout_seconds: int) -> None: ...

    def click(self, selector: str, *, timeout_seconds: int) -> None: ...

    def select(self, selector: str, value: str, *, timeout_seconds: int) -> None: ...

    def value_of(self, selector: str) -> str | None: ...

    def text_of(self, selector: str) -> str | None: ...

    def visible(self, selector: str) -> bool: ...

    def screenshot(self) -> bytes: ...

    def assert_precondition(self, assertion: str, equals: str | None) -> bool: ...

    def retry_duplicate_filename(
        self, *, claim_id_suffix: str, collision_number: int, timeout_seconds: int
    ) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class StepFrame:
    """One captured screenshot, before or after a single executed step."""

    step_id: str
    phase: str
    content: bytes


@dataclass
class ExecutionTrace:
    """What the executor durably observed, independent of any outcome mapping."""

    frames: list[StepFrame] = field(default_factory=list)
    last_completed_step: str | None = None
    write_ids: list[str] = field(default_factory=list)
    inputs: dict[str, Any] = field(default_factory=dict)


class ExactExecutor:
    """Drive one versioned click path exactly, or halt."""

    def __init__(
        self,
        session: BrowserSession,
        click_path: ClickPath,
        *,
        timeouts: Any,
        heartbeat: Callable[[], None] | None = None,
        on_frame: Callable[[StepFrame], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.session = session
        self.click_path = click_path
        self.timeouts = timeouts
        self.heartbeat = heartbeat or (lambda: None)
        self.on_frame = on_frame
        self.sleep = sleep
        self.trace = ExecutionTrace()
        self._values: dict[str, str] = {}

    # -- framing ---------------------------------------------------------------

    def _capture(self, step_id: str, phase: str) -> None:
        try:
            content = self.session.screenshot()
        except Exception:  # noqa: BLE001 - a dead browser cannot photograph itself
            return
        frame = StepFrame(step_id=step_id, phase=phase, content=content)
        self.trace.frames.append(frame)
        if self.on_frame is not None:
            self.on_frame(frame)

    # -- assertions ------------------------------------------------------------

    def _assert_preconditions(self) -> None:
        for precondition in self.click_path.preconditions:
            if not self.session.assert_precondition(precondition.assertion, precondition.equals):
                raise SelectorDrift(None, f"precondition_{precondition.assertion}")

    def _resolve(self, step: Step) -> None:
        """Exactly one element, or halt. There is no second attempt."""

        count = self.session.count(step.selector)
        if count == 0:
            raise SelectorDrift(step.id, "selector_no_match")
        if count > 1:
            raise SelectorDrift(step.id, "selector_multiple_matches")

    def _postcondition_passes(self, step: Step) -> bool:
        postcondition = step.postcondition
        if postcondition is None:
            return True
        kind = postcondition.kind
        if kind == "visible":
            return self.session.visible(postcondition.selector)
        elif kind == "absent":
            return self.session.count(postcondition.selector) == 0
        elif kind == "exact_value":
            return self.session.value_of(postcondition.selector) == self._expected(step)
        elif kind == "text_contains":
            text = self.session.text_of(postcondition.selector) or ""
            return str(postcondition.value) in text
        return False  # pragma: no cover - the loader closes the vocabulary

    def _assert_postcondition(self, step: Step) -> None:
        if self._postcondition_passes(step):
            return
        if (
            step.is_external_write
            and any(
                failure.handler == "slow_reflection"
                for failure in self.click_path.known_failures
            )
        ):
            elapsed = 0
            interval = self.timeouts.reflection_poll_seconds
            while elapsed < self.timeouts.reflection_timeout_seconds:
                self.heartbeat()
                self.sleep(interval)
                elapsed += interval
                if self._postcondition_passes(step):
                    return
            raise ReflectionTimeout(step.id)
        kind = step.postcondition.kind if step.postcondition is not None else "missing"
        raise SelectorDrift(step.id, f"postcondition_{kind}")

    def _expected(self, step: Step) -> str | None:
        return self._values.get(step.id)

    # -- execution -------------------------------------------------------------

    def run(self, values: dict[str, str]) -> ExecutionTrace:
        """Execute every declared step in order. Any drift halts immediately."""

        self._values = dict(values)
        self._assert_preconditions()
        self._run_from(0)
        return self.trace

    def _run_from(self, start: int) -> None:
        for step in self.click_path.steps[start:]:
            self._execute_step(step)

    def _execute_step(self, step: Step) -> None:
        timeout = self.timeouts.step_timeout(step.timeout_class or "default")
        self.heartbeat()
        self._capture(step.id, "before")
        try:
            self._resolve(step)
            if step.write_id is not None and step.write_id not in self.trace.write_ids:
                # Recorded *before* dispatch: once the action is issued the
                # target may have written, whatever happens next.
                self.trace.write_ids.append(step.write_id)
            self._act(step, timeout)
            self._assert_postcondition(step)
        except (SelectorDrift, TargetKnownFailure, ReflectionTimeout):
            self._capture(step.id, "failure")
            raise
        self._capture(step.id, "after")
        self.trace.last_completed_step = step.id

    def retry_duplicate_filename(self, step_id: str, *, claim_id_suffix: str) -> ExecutionTrace:
        """Run the one captured EDMS duplicate-filename recovery, exactly once."""

        index = next(
            (index for index, step in enumerate(self.click_path.steps) if step.id == step_id),
            None,
        )
        if index is None:
            raise SelectorDrift(step_id, "duplicate_failure_step_unknown")
        step = self.click_path.steps[index]
        timeout = self.timeouts.step_timeout(step.timeout_class or "default")
        self.heartbeat()
        self._capture(step.id, "before")
        try:
            self.session.retry_duplicate_filename(
                claim_id_suffix=claim_id_suffix,
                collision_number=1,
                timeout_seconds=timeout,
            )
            self._assert_postcondition(step)
        except (SelectorDrift, TargetKnownFailure, ReflectionTimeout):
            self._capture(step.id, "failure")
            raise
        self._capture(step.id, "after")
        self.trace.last_completed_step = step.id
        self._run_from(index + 1)
        return self.trace

    def _act(self, step: Step, timeout: int) -> None:
        if step.action == "click":
            self.session.click(step.selector, timeout_seconds=timeout)
            return
        value = self._values.get(step.id)
        if value is None:
            raise SelectorDrift(step.id, "payload_value_missing")
        if step.action == "fill":
            self.session.fill(step.selector, value, timeout_seconds=timeout)
            return
        self.session.select(step.selector, value, timeout_seconds=timeout)

    # -- reconciliation reads --------------------------------------------------

    def read_inputs(self) -> dict[str, str | None]:
        """Re-read every declared input from the target for reconciliation."""

        return {
            entry.step_id: self.session.value_of(entry.selector)
            for entry in self.click_path.reconciliation
        }

    def read_outputs(self) -> dict[str, str | None]:
        """Read every declared target output from its own captured selector."""

        outputs: dict[str, str | None] = {}
        for entry in self.click_path.readback:
            if entry.selector is None:
                # An executable definition cannot reach here; the loader refuses
                # a live RPA readback with no captured selector (#297).
                continue
            outputs[entry.capture] = self.session.text_of(entry.selector)
        return outputs


__all__ = [
    "BrowserSession",
    "CREDENTIAL_SCREEN",
    "ExactExecutor",
    "ExecutionTrace",
    "ReflectionTimeout",
    "SelectorDrift",
    "StepFrame",
    "TargetKnownFailure",
]
