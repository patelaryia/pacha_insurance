"""PRD-07 vendor registry model, seed loader, and authenticated read route."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Header, Query
from sqlalchemy import JSON, Boolean, CheckConstraint, Text, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from claim_core import Base, ClaimCoreError

VENDOR_KINDS = frozenset({"assessor", "garage", "supplier", "salvage_yard"})
EMAILS_VALUE = JSON().with_variant(postgresql.ARRAY(Text()), "postgresql")
FEE_VALUE = JSON().with_variant(postgresql.JSONB(), "postgresql")


class Vendor(Base):
    """A never-deleted external firm available for explicit officer selection."""

    __tablename__ = "vendors"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('assessor', 'garage', 'supplier', 'salvage_yard')",
            name="ck_vendors_kind",
        ),
        {
            "comment": (
                "Append-only-by-policy PRD-07 vendor registry; deactivate, never delete."
            )
        },
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, comment="Stable pack/vendor id")
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    emails: Mapped[list[str]] = mapped_column(EMAILS_VALUE, nullable=False)
    fee_schedule: Mapped[dict[str, Any]] = mapped_column(FEE_VALUE, nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )


def validate_vendors(rows: Any) -> list[dict[str, Any]]:
    """Validate configured seed rows without inventing missing firms or fees."""

    if not isinstance(rows, list):
        raise ValueError("vendors must be a list")
    loaded: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict) or set(raw) != {
            "id",
            "kind",
            "name",
            "emails",
            "fee_schedule",
            "active",
        }:
            raise ValueError("vendor rows require the exact PRD-07 fields")
        vendor_id = raw["id"]
        emails = raw["emails"]
        fees = raw["fee_schedule"]
        if (
            not isinstance(vendor_id, str)
            or not vendor_id
            or vendor_id in seen
            or raw["kind"] not in VENDOR_KINDS
            or not isinstance(raw["name"], str)
            or not raw["name"].strip()
            or not isinstance(emails, list)
            or not all(isinstance(value, str) and value.strip() for value in emails)
            or not isinstance(fees, dict)
            or not all(
                isinstance(value, int) and not isinstance(value, bool) and value >= 0
                for value in fees.values()
            )
            or not isinstance(raw["active"], bool)
        ):
            raise ValueError(f"invalid vendor seed {vendor_id!r}")
        if raw["kind"] == "assessor" and not emails:
            raise ValueError(f"assessor vendor {vendor_id!r} requires a captured email")
        seen.add(vendor_id)
        loaded.append(
            {
                "id": vendor_id,
                "kind": raw["kind"],
                "name": raw["name"].strip(),
                "emails": [value.strip() for value in emails],
                "fee_schedule": dict(fees),
                "active": raw["active"],
            }
        )
    return loaded


class VendorRegistry:
    """Idempotent seed and deterministic active-vendor reads."""

    def __init__(self, app: Any, rows: list[dict[str, Any]]) -> None:
        self.app = app
        self.sessions = sessionmaker(bind=app.state.engine, expire_on_commit=False)
        self._seed(rows)

    def _seed(self, rows: list[dict[str, Any]]) -> None:
        """Upsert the pack-authoritative registry, including activation state."""

        with self.sessions.begin() as session:
            for values in rows:
                row = session.get(Vendor, values["id"])
                if row is None:
                    session.add(Vendor(**values))
                    continue
                for key in ("kind", "name", "emails", "fee_schedule", "active"):
                    setattr(row, key, values[key])

    def active_assessors(self, vendor_ids: list[str]) -> list[Vendor]:
        with self.sessions() as session:
            rows = list(
                session.scalars(
                    select(Vendor)
                    .where(
                        Vendor.id.in_(vendor_ids),
                        Vendor.kind == "assessor",
                        Vendor.active.is_(True),
                    )
                    .order_by(Vendor.id)
                )
            )
            for row in rows:
                session.expunge(row)
        return rows

    def list_active(self, kind: str | None) -> list[dict[str, Any]]:
        with self.sessions() as session:
            query = select(Vendor).where(Vendor.active.is_(True))
            if kind is not None:
                query = query.where(Vendor.kind == kind)
            rows = list(session.scalars(query.order_by(Vendor.id)))
        return [
            {
                "id": row.id,
                "kind": row.kind,
                "name": row.name,
                "emails": list(row.emails),
                "fee_schedule": dict(row.fee_schedule),
                "active": row.active,
            }
            for row in rows
        ]


def build_router(app: Any, registry: VendorRegistry) -> APIRouter:
    router = APIRouter()

    @router.get("/vendors")
    def vendors(
        kind: Literal["assessor", "garage", "supplier", "salvage_yard"] | None = Query(
            default=None
        ),
        x_actor: str = Header(alias="X-Actor"),
    ) -> dict[str, Any]:
        role = app.state.review_queue.service.authorizer.role(x_actor)
        if role is None:
            raise ClaimCoreError(403, "FORBIDDEN_ROLE", "Actor has no configured human role")
        return {"vendors": registry.list_active(kind)}

    return router


__all__ = ["Vendor", "VendorRegistry", "build_router", "validate_vendors"]
