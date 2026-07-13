"""Boundary unit coverage for Packet-05 live document intelligence."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def test_anthropic_adapter_redacts_audit_and_accepts_mapping_tool_blocks():
    from doc_intel.anthropic_client import AnthropicModelClient

    class Messages:
        def create(self, **kwargs):
            assert kwargs["max_tokens"] == 123
            return SimpleNamespace(
                content=[{"type": "tool_use", "input": {"visible": True}}],
                usage=SimpleNamespace(input_tokens=10, output_tokens=5),
                model=kwargs["model"],
            )

    class Ledger:
        def __init__(self):
            self.detail = None

        def record_model_call(self, detail):
            self.detail = detail

    ledger = Ledger()
    client = AnthropicModelClient(
        SimpleNamespace(messages=Messages()),
        config={
            "tiers": {
                "MODEL_LIGHT": {
                    "model_id": "light",
                    "input_usd_per_mtok": "1",
                    "output_usd_per_mtok": "5",
                    "max_output_tokens": 123,
                }
            },
            "audit_redacted_keys": ["value"],
        },
        ledger=ledger,
    )
    result = client.structured_call(
        tier="MODEL_LIGHT",
        schema={"type": "object"},
        inputs={"task": "vision_crop_verify", "value": "secret"},
    )
    assert result["data"] == {"visible": True}
    assert ledger.detail["request"]["value"] == "__redacted__"
    assert result["cost_usd"] > 0


def test_anthropic_adapter_fail_closed_configuration_and_transport():
    from doc_intel.anthropic_client import AnthropicModelClient
    from doc_intel.llm import ModelTransportError

    empty = AnthropicModelClient(
        SimpleNamespace(messages=None), config={"tiers": {}}, ledger=None
    )
    with pytest.raises(ValueError, match="not configured"):
        empty.structured_call(tier="MODEL_LIGHT", schema={}, inputs={})

    pending = AnthropicModelClient(
        SimpleNamespace(messages=None),
        config={"tiers": {"MODEL_LIGHT": {"model_id": "pending_capture"}}},
        ledger=None,
    )
    with pytest.raises(ValueError, match="no usable model id"):
        pending.structured_call(tier="MODEL_LIGHT", schema={}, inputs={})

    class Messages:
        def create(self, **_kwargs):
            error = RuntimeError("busy")
            error.status_code = 503
            raise error

    configured = AnthropicModelClient(
        SimpleNamespace(messages=Messages()),
        config={"tiers": {"MODEL_LIGHT": {"model_id": "light"}}},
        ledger=None,
    )
    with pytest.raises(ModelTransportError):
        configured.structured_call(tier="MODEL_LIGHT", schema={}, inputs={})


@pytest.mark.parametrize(
    "bbox",
    [None, [0, 0, 1], [0, 0, 0, 1], [-0.1, 0, 1, 1], [0, 0, float("inf"), 1]],
)
def test_vision_bbox_validation_rejects_malformed_values(bbox):
    from doc_intel.vision import normalized_bbox

    assert normalized_bbox(bbox) is None


def test_split_boundary_validation_rejects_single_gap_and_trailing_pages():
    from claim_core import ClaimCoreError
    from doc_intel.split import validate_boundaries

    cases = [
        [{"start_page": 1, "end_page": 3}],
        [{"start_page": 1, "end_page": 1}, {"start_page": 3, "end_page": 3}],
        [{"start_page": 1, "end_page": 1}, {"start_page": 2, "end_page": 2}],
    ]
    for boundaries in cases:
        with pytest.raises(ClaimCoreError) as captured:
            validate_boundaries(boundaries, 3)
        assert captured.value.code == "INVALID_SPLIT_BOUNDARY"


def test_consistency_missing_triggers_and_unknown_expression_fail_closed():
    from doc_intel.consistency import evaluate_observations

    assert evaluate_observations({}, definitions=[{
        "id": "CC-1",
        "trigger_docs": ["claim", "logbook"],
        "expression": "all_registrations_equal",
        "severity": "block-pack",
    }]) == []
    with pytest.raises(ValueError, match="unknown consistency expression"):
        evaluate_observations(
            {"claim": {}},
            definitions=[{
                "id": "CC-X",
                "trigger_docs": ["claim"],
                "expression": "invented",
                "severity": "flag",
            }],
        )


def test_named_stage_task_calls_configured_shared_engine():
    from doc_intel.tasks import PIPELINE_TASKS, configure_runtime

    class Engine:
        def process_stage(self, document_id, stage):
            return {"document_id": document_id, "stage": stage}

    configure_runtime(Engine())
    assert PIPELINE_TASKS["CITE"].run("doc-1") == {
        "document_id": "doc-1",
        "stage": "CITE",
    }


def test_shared_gate_accepts_only_verified_vision_citation(tmp_path):
    from fastapi.testclient import TestClient

    from claim_core.app import create_app

    headers = {"X-Actor": "agent:doc_intel"}
    client = TestClient(create_app(f"sqlite:///{tmp_path}/vision-gate.db"))
    claim_id = client.post(
        "/claims",
        json={"lob": "motor", "pack_version": "motor@1.3.0"},
        headers=headers,
    ).json()["id"]
    document_id = client.post(
        f"/claims/{claim_id}/documents",
        files={"file": ("claim.pdf", b"immutable", "application/pdf")},
        data={"source_channel": "test", "source_ref": "vision"},
        headers=headers,
    ).json()["id"]
    source_ref = {
        "document_id": document_id,
        "page": 1,
        "bbox": [0.1, 0.1, 0.5, 0.2],
        "citation_mode": "vision_bbox",
        "vision_verified": True,
    }
    response = client.patch(
        f"/claims/{claim_id}/fields",
        json={"writes": [{
            "path": "policy.number",
            "value": "POL-1",
            "value_type": "string",
            "source_type": "extraction",
            "source_ref": source_ref,
            "confidence": 0.9,
            "verification_state": "extracted",
        }]},
        headers=headers,
    )
    assert response.status_code == 200
    source_ref["anchor_text"] = "POL-1"
    response = client.patch(
        f"/claims/{claim_id}/fields",
        json={"writes": [{
            "path": "policy.number",
            "value": "POL-2",
            "value_type": "string",
            "source_type": "extraction",
            "source_ref": source_ref,
            "confidence": 0.9,
            "verification_state": "extracted",
        }]},
        headers=headers,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "CITATION_REQUIRED"
