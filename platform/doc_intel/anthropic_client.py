"""Anthropic structured tool-use adapter with injected SDK transport."""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from doc_intel.llm import ModelTransportError


def _content_input(block: Any) -> dict[str, Any] | None:
    block_type = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
    value = block.get("input") if isinstance(block, dict) else getattr(block, "input", None)
    return value if block_type == "tool_use" and isinstance(value, dict) else None


def _redact(value: Any, redacted_keys: frozenset[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: "__redacted__" if key in redacted_keys else _redact(item, redacted_keys)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, redacted_keys) for item in value]
    return value


class AnthropicModelClient:
    """Implement the provider-neutral ModelClient protocol via forced tool use."""

    def __init__(self, sdk_client: Any, *, config: Mapping[str, Any], ledger: Any) -> None:
        self.sdk_client = sdk_client
        self.config = dict(config)
        self.ledger = ledger

    def structured_call(self, *, tier: str, schema: dict, inputs: dict) -> dict:
        tier_config = self.config.get("tiers", {}).get(tier)
        if not isinstance(tier_config, dict):
            raise ValueError(f"model tier {tier!r} is not configured")
        model_id = inputs.get("_model_id", tier_config.get("model_id"))
        if not isinstance(model_id, str) or not model_id or model_id == "pending_capture":
            raise ValueError(f"model tier {tier!r} has no usable model id")
        tool_name = "pacha_structured_result"
        provider_inputs = {key: value for key, value in inputs.items() if key != "_model_id"}
        try:
            response = self.sdk_client.messages.create(
                model=model_id,
                temperature=0,
                max_tokens=int(tier_config.get("max_output_tokens", 4096)),
                messages=[
                    {
                        "role": "user",
                        "content": json.dumps(provider_inputs, ensure_ascii=False, default=str),
                    }
                ],
                tools=[
                    {
                        "name": tool_name,
                        "description": "Return validated JSON",
                        "input_schema": schema,
                    }
                ],
                tool_choice={"type": "tool", "name": tool_name},
            )
        except Exception as error:
            status = getattr(error, "status_code", None)
            if isinstance(error, (TimeoutError, ConnectionError)) or status == 429 or (
                isinstance(status, int) and status >= 500
            ):
                raise ModelTransportError(str(error)) from error
            raise
        data = next(
            (parsed for block in response.content if (parsed := _content_input(block)) is not None),
            None,
        )
        if data is None:
            data = {}
        usage = response.usage
        input_tokens = int(getattr(usage, "input_tokens", 0))
        output_tokens = int(getattr(usage, "output_tokens", 0))
        input_price = Decimal(str(tier_config["input_usd_per_mtok"]))
        output_price = Decimal(str(tier_config["output_usd_per_mtok"]))
        cost = (
            Decimal(input_tokens) * input_price + Decimal(output_tokens) * output_price
        ) / Decimal(1_000_000)
        if self.ledger is not None:
            redacted_keys = frozenset(self.config.get("audit_redacted_keys", ()))
            self.ledger.record_model_call(
                {
                    "task": inputs.get("task"),
                    "tier": tier,
                    "model_id": getattr(response, "model", model_id),
                    "request": _redact(provider_inputs, redacted_keys),
                    "response": _redact(data, redacted_keys),
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cost_usd": str(cost),
                }
            )
        return {
            "data": data,
            "cost_usd": float(cost),
            "model_id": getattr(response, "model", model_id),
        }
