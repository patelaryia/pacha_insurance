"""AR-4 LIGHT production classifier for unmatched mailbox messages."""

from __future__ import annotations

from typing import Any

from doc_intel.llm import ModelWrapper

CLASSES = (
    "new_intimation",
    "multi_intimation",
    "claim_related_unmatched",
    "not_a_claim",
    "unclear",
)
SCHEMA = {
    "type": "object",
    "required": ["class", "confidence"],
    "properties": {
        "class": {"enum": list(CLASSES)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "additionalProperties": False,
}


class MailboxClassifier:
    """Call the configured model only through the shared structured wrapper."""

    def __init__(self, app: Any, config: dict[str, Any]) -> None:
        self.app = app
        self.config = config

    def classify(self, message: dict[str, Any]) -> dict[str, Any]:
        client = self.app.state.eval_harness.model_client
        if client is None:
            raise RuntimeError("mailbox classifier model client is not configured")
        classifier = self.config["classifier"]
        result = ModelWrapper(
            client,
            budget_ceiling_usd=float(classifier["max_cost_usd"]),
        ).structured_call(
            tier=classifier["tier"],
            schema=SCHEMA,
            inputs={
                "purpose": classifier["purpose"],
                "prompt": classifier["prompt"],
                "message": {
                    "from_addr": message["from_addr"],
                    "subject": message["subject"],
                    "body_text": message["body_text"],
                    "attachment_names": [
                        item.get("filename") for item in message.get("attachments", [])
                    ],
                },
            },
        )
        self.app.state.claim_service.record_model_call(
            {
                "task": "mailbox_triage",
                "tier": classifier["tier"],
                "model_id": result["model_id"],
                "cost_usd": result["cost_usd"],
                "graph_message_id": message["graph_message_id"],
            }
        )
        return dict(result["data"])


__all__ = ["CLASSES", "MailboxClassifier"]
