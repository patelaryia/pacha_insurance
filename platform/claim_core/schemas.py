"""Pydantic request and response schemas for Packet 01."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ClaimCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lob: str = Field(min_length=1)
    pack_version: str = Field(min_length=1)


class ClaimResponse(BaseModel):
    id: str
    lob: str
    pack_version: str
    status: str
    substatus: str | None
    external_refs: dict[str, Any]
    assigned_to: str | None
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None


class FieldWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    value: Any
    value_type: str
    source_type: str
    source_ref: dict[str, Any] | None = None
    confidence: Decimal | None = Field(default=None, ge=0, le=1)
    verification_state: str
    pii_class: str | None = None


class FieldWriteBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    writes: list[FieldWrite] = Field(min_length=1)


class FieldWriteResult(BaseModel):
    path: str
    field_id: str
    version: int


class FieldWriteResponse(BaseModel):
    results: list[FieldWriteResult]


class HydratedField(BaseModel):
    value: Any
    value_type: str
    version: int
    verification_state: str
    source_type: str
    source_ref: dict[str, Any] | None
    confidence: float | None
    created_by: str
    created_at: datetime


class HydratedClaim(ClaimResponse):
    fields: dict[str, HydratedField]
    blocked_reasons: list[str]


class TransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: str = Field(min_length=1)
    payload: dict[str, Any] | None = None


class DeclineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1)


class SubstatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    substatus: str | None


class ClaimStateSummary(BaseModel):
    id: str
    status: str
    substatus: str | None


class ApprovalRequiredResponse(BaseModel):
    code: str


class TimelineEvent(BaseModel):
    id: str
    type: str
    payload: dict[str, Any]
    actor: str
    correlation_id: str | None
    occurred_at: datetime


class TimelineResponse(BaseModel):
    events: list[TimelineEvent]


class ErrorResponse(BaseModel):
    code: str
    detail: str
