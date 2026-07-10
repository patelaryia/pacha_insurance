"""Provider-neutral structured-model seam with ED-4a failure semantics."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from datetime import timedelta
from typing import Any, Protocol, TypedDict

from jsonschema import ValidationError, validate

from doc_intel.settings import DEFAULTS


class ModelResult(TypedDict):
    data: dict[str, Any]
    cost_usd: float
    model_id: str


class ModelClient(Protocol):
    def structured_call(
        self, *, tier: str, schema: dict, inputs: dict
    ) -> ModelResult: ...


class ModelError(RuntimeError):
    """Base class for model-wrapper failures."""


class ModelTransportError(ModelError):
    """Retryable provider transport failure."""


class ModelSchemaError(ModelError):
    """Structured output remained invalid after one regeneration."""


class ModelBudgetExceeded(ModelError):
    """The configured spend ceiling has already been reached."""


class ModelUnavailable(ModelError):
    """The provider remained unavailable after bounded retry."""


DEFAULT_RETRY_CONFIG = DEFAULTS["model_wrapper"]


class ModelWrapper:
    """Validate outputs, meter spend, and isolate transient provider failures."""

    def __init__(
        self,
        client: ModelClient,
        *,
        budget_ceiling_usd: float | None = None,
        config: Mapping[str, Any] | None = None,
        clock: Callable[[], Any] | None = None,
    ) -> None:
        self.client = client
        self.budget_ceiling_usd = budget_ceiling_usd
        self.config = {**DEFAULT_RETRY_CONFIG, **dict(config or {})}
        self._clock = clock or time.monotonic
        configured_sleep = self.config.get("sleep")
        if configured_sleep is not None:
            self._sleep = configured_sleep
        elif clock is not None and callable(getattr(clock, "sleep", None)):
            self._sleep = clock.sleep
        elif clock is not None and callable(getattr(clock, "advance", None)):
            self._sleep = lambda seconds: clock.advance(seconds=seconds)
        else:
            self._sleep = time.sleep
        self.spent_usd = 0.0

    def _check_budget(self) -> None:
        if (
            self.budget_ceiling_usd is not None
            and self.spent_usd >= self.budget_ceiling_usd
        ):
            raise ModelBudgetExceeded("model budget ceiling has been reached")

    def _inputs_for_attempt(self, tier: str, inputs: dict, attempt: int) -> dict:
        tier_config = self.config.get("tiers", {}).get(tier, {})
        switch_after = int(self.config["fallback_after_attempt"])
        model_key = "fallback_model_id" if attempt > switch_after else "model_id"
        model_id = tier_config.get(model_key)
        if model_id is None:
            return inputs
        call_inputs = deepcopy(inputs)
        call_inputs["_model_id"] = model_id
        return call_inputs

    @staticmethod
    def _validated_result(raw: object, schema: dict) -> ModelResult:
        if not isinstance(raw, dict):
            raise ValidationError("model result must be an object")
        data = raw.get("data")
        cost = raw.get("cost_usd")
        model_id = raw.get("model_id")
        if not isinstance(data, dict):
            raise ValidationError("model result data must be an object")
        if not isinstance(cost, (int, float)) or isinstance(cost, bool):
            raise ValidationError("model result cost_usd must be numeric")
        if not isinstance(model_id, str):
            raise ValidationError("model result model_id must be a string")
        validate(instance=data, schema=schema)
        return {"data": data, "cost_usd": float(cost), "model_id": model_id}

    @staticmethod
    def _elapsed_seconds(start: Any, end: Any) -> float:
        elapsed = end - start
        if isinstance(elapsed, timedelta):
            return elapsed.total_seconds()
        return float(elapsed)

    def _one_call(self, *, tier: str, schema: dict, inputs: dict) -> ModelResult:
        start = self._clock()
        backoff = float(self.config["initial_backoff_seconds"])
        max_attempts = int(self.config["max_attempts"])
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            self._check_budget()
            try:
                raw = self.client.structured_call(
                    tier=tier,
                    schema=schema,
                    inputs=self._inputs_for_attempt(tier, inputs, attempt),
                )
            except (ModelTransportError, TimeoutError, ConnectionError) as error:
                last_error = error
                elapsed = self._elapsed_seconds(start, self._clock())
                if attempt >= max_attempts or elapsed + backoff > float(
                    self.config["max_elapsed_seconds"]
                ):
                    break
                self._sleep(backoff)
                backoff = min(backoff * 2, float(self.config["max_backoff_seconds"]))
                continue
            try:
                result = self._validated_result(raw, schema)
            except ValidationError:
                if (
                    isinstance(raw, dict)
                    and isinstance(raw.get("cost_usd"), (int, float))
                    and not isinstance(raw.get("cost_usd"), bool)
                ):
                    self.spent_usd += float(raw["cost_usd"])
                raise
            self.spent_usd += result["cost_usd"]
            return result
        raise ModelUnavailable("model provider unavailable after bounded retry") from last_error

    def structured_call(self, *, tier: str, schema: dict, inputs: dict) -> ModelResult:
        """Make a structured call; schema-invalid output regenerates exactly once."""

        for regeneration in range(2):
            try:
                return self._one_call(tier=tier, schema=schema, inputs=inputs)
            except ValidationError as error:
                if regeneration == 1:
                    raise ModelSchemaError("structured output failed schema validation") from error
        raise AssertionError("unreachable regeneration state")


class FakeModelClient:
    """Deterministic queued client used by unit and integration tests."""

    def __init__(self, responses: Iterable[ModelResult | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def structured_call(self, *, tier: str, schema: dict, inputs: dict) -> ModelResult:
        self.calls.append({"tier": tier, "schema": schema, "inputs": inputs})
        if not self.responses:
            raise AssertionError("FakeModelClient response queue is empty")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)


class FlakyModelClient:
    """Raise retryable failures before delegating to another fake client."""

    def __init__(
        self,
        client: ModelClient | Iterable[ModelResult | Exception],
        failures: int,
    ) -> None:
        self.client = (
            client if hasattr(client, "structured_call") else FakeModelClient(client)
        )
        self.failures = failures
        self.calls = 0

    def structured_call(self, *, tier: str, schema: dict, inputs: dict) -> ModelResult:
        self.calls += 1
        if self.calls <= self.failures:
            raise ModelTransportError("injected transient failure")
        return self.client.structured_call(tier=tier, schema=schema, inputs=inputs)
