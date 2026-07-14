"""Review queue reads and the versioned, human-authorised resolution engine."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import column, null, select, table, text
from sqlalchemy.orm import Session, sessionmaker

from claim_core import ClaimCoreError, FieldWrite
from review_queue.contracts import ContractRegistry
from review_queue.models import ReviewItem
from review_queue.rbac import Authorizer

RESOLUTIONS = {
    "approve": "approved",
    "edit_approve": "edited",
    "reject": "rejected",
}
CLAIMS = table("claims", column("id"), column("assigned_to"))
DECLINE_APPROVAL_STATUS_VALUES = frozenset(
    {
        "AWAITING_DOCS",
        "IN_ASSESSMENT",
        "REPORT_RECEIVED",
        "REGISTERED",
        "RESERVED",
        "PACK_READY",
    }
)


class ReviewService:
    def __init__(
        self,
        app: Any,
        sessions: sessionmaker,
        contracts: ContractRegistry,
        authorizer: Authorizer,
    ) -> None:
        self.app = app
        self.sessions = sessions
        self.contracts = contracts
        self.authorizer = authorizer

    @staticmethod
    def _not_found(review_id: str) -> ClaimCoreError:
        return ClaimCoreError(404, "REVIEW_NOT_FOUND", f"Review item {review_id} was not found")

    def _item(self, session: Session, review_id: str, *, lock: bool = False) -> ReviewItem:
        query = select(ReviewItem).where(ReviewItem.id == review_id)
        if lock and session.bind is not None and session.bind.dialect.name == "postgresql":
            query = query.with_for_update()
        item = session.scalar(query)
        if item is None:
            raise self._not_found(review_id)
        return item

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        return None if value is None else value.isoformat()

    def _sla(self, claim_id: str | None) -> list[dict[str, Any]]:
        if claim_id is None:
            return []
        with self.app.state.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, definition_id, state, started_at, stopped_at, warn_at, "
                    "breach_at, started_by_event, stopped_by_event FROM sla_clocks "
                    "WHERE claim_id = :claim_id ORDER BY started_at, id"
                ),
                {"claim_id": claim_id},
            ).mappings()
            return [
                {
                    key: self._iso(value) if isinstance(value, datetime) else value
                    for key, value in dict(row).items()
                }
                for row in rows
            ]

    def serialise(self, item: ReviewItem) -> dict[str, Any]:
        return {
            "id": item.id,
            "claim_id": item.claim_id,
            "type": item.type,
            "subtype": item.subtype,
            "status": item.status,
            "assigned_to": item.assigned_to,
            "payload": item.payload,
            "source_event_id": item.source_event_id,
            "created_at": self._iso(item.created_at),
            "resolved_at": self._iso(item.resolved_at),
            "resolved_by": item.resolved_by,
            "resolution": item.resolution,
            "resolution_payload": item.resolution_payload,
            "resolution_schema_version": item.resolution_schema_version,
            "sla": self._sla(item.claim_id),
        }

    def _read_role(self, actor: str) -> str:
        role = self.authorizer.role(actor)
        if role is None:
            raise ClaimCoreError(403, "FORBIDDEN_ROLE", "Actor has no configured human role")
        return role

    def list_items(
        self,
        *,
        actor: str,
        scope: str,
        type_name: str | None,
        status: str | None,
        claim_id: str | None,
    ) -> list[dict[str, Any]]:
        self._read_role(actor)
        if scope not in {"mine", "pool"}:
            raise ClaimCoreError(422, "VALUE_TYPE_MISMATCH", "scope must be mine or pool")
        with self.sessions() as session:
            query = select(ReviewItem)
            if scope == "mine":
                query = query.where(
                    ReviewItem.claim_id.in_(
                        select(CLAIMS.c.id).where(CLAIMS.c.assigned_to == actor)
                    )
                )
            if type_name is not None:
                query = query.where(ReviewItem.type == type_name)
            if status is not None:
                query = query.where(ReviewItem.status == status)
            if claim_id is not None:
                query = query.where(ReviewItem.claim_id == claim_id)
            items = list(session.scalars(query.order_by(ReviewItem.created_at, ReviewItem.id)))
            for item in items:
                session.expunge(item)
        return [self.serialise(item) for item in items]

    def get_item(self, review_id: str, *, actor: str) -> dict[str, Any]:
        self._read_role(actor)
        with self.sessions() as session:
            item = self._item(session, review_id)
            session.expunge(item)
        return self.serialise(item)

    def _deny(self, item: ReviewItem, actor: str, code: str) -> None:
        if code == "RESOLUTION_BLOCKED_ON_INPUTS":
            raise ClaimCoreError(409, code, "Resolution is blocked on required claim inputs")
        with self.sessions.begin() as session:
            self.app.state.record_event(
                session,
                claim_id=item.claim_id,
                event_type="authz.denied",
                payload={
                    "review_id": item.id,
                    "type": item.type,
                    "actor": actor,
                    "code": code,
                },
                actor=actor,
                correlation_id=item.id,
            )
        raise ClaimCoreError(403, code, "Actor is not authorised to resolve this review item")

    def _band_amount(self, item: ReviewItem, path: str | None, actor: str) -> int | None:
        if path is None or item.claim_id is None:
            return None
        _claim, fields, _blocked = self.app.state.claim_service.hydrate_claim(
            item.claim_id, actor, paths=[path]
        )
        field = fields.get(path)
        if field is None or not isinstance(field.value, int) or isinstance(field.value, bool):
            return None
        return field.value

    @staticmethod
    def _validate_action_payload(action: str, type_name: str, payload: dict[str, Any]) -> None:
        if action == "reject":
            reason = payload.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise ClaimCoreError(422, "PAYLOAD_INVALID", "Reject requires a free-text reason")
        if action == "edit_approve" and type_name == "FIELD_VERIFY":
            corrected = payload.get("corrected_fields")
            if not isinstance(corrected, dict) or not corrected:
                raise ClaimCoreError(
                    422,
                    "PAYLOAD_INVALID",
                    "FIELD_VERIFY edit requires corrected_fields",
                )
        if action == "edit_approve" and type_name == "DOC_SPLIT":
            boundaries = payload.get("boundaries")
            if not isinstance(boundaries, list) or not boundaries:
                raise ClaimCoreError(422, "PAYLOAD_INVALID", "DOC_SPLIT edit requires boundaries")

    def _field_verify(self, item: ReviewItem, payload: dict[str, Any], actor: str) -> None:
        corrected = payload["corrected_fields"]
        paths = list(corrected)
        if item.claim_id is None:
            raise ClaimCoreError(409, "RESOLUTION_BLOCKED_ON_INPUTS", "Review has no claim")
        _claim, current, _blocked = self.app.state.claim_service.hydrate_claim(
            item.claim_id, actor, paths=paths
        )
        if set(current) != set(paths):
            raise ClaimCoreError(
                409,
                "RESOLUTION_BLOCKED_ON_INPUTS",
                "A corrected field has no current value to verify",
            )
        writes = [
            FieldWrite(
                path=path,
                value=value,
                value_type=current[path].value_type,
                source_type="human",
                source_ref={"user_id": actor, "review_item_id": item.id},
                verification_state="human_verified",
            )
            for path, value in corrected.items()
        ]
        self.app.state.claim_service.write_fields(item.claim_id, writes, actor)

    def _doc_split(self, item: ReviewItem, payload: dict[str, Any], actor: str) -> None:
        engine = getattr(self.app.state, "doc_intel", None)
        document_id = item.payload.get("document_id")
        if engine is None or not isinstance(document_id, str) or not document_id:
            raise ClaimCoreError(
                409,
                "RESOLUTION_BLOCKED_ON_INPUTS",
                "Document split engine or document id is unavailable",
            )
        engine.apply_human_boundaries(document_id, boundaries=payload["boundaries"], actor=actor)

    def _side_effect_before(
        self, item: ReviewItem, action: str, payload: dict[str, Any], actor: str
    ) -> None:
        if action != "edit_approve":
            return
        if item.type == "FIELD_VERIFY":
            self._field_verify(item, payload, actor)
        elif item.type == "DOC_SPLIT":
            self._doc_split(item, payload, actor)

    def _assert_open_locked(self, review_id: str) -> None:
        with self.sessions.begin() as session:
            item = self._item(session, review_id, lock=True)
            if item.status != "open":
                raise ClaimCoreError(409, "ALREADY_RESOLVED", "Review item is no longer open")

    def _validate_decline_claim_state(self, item: ReviewItem, actor: str) -> None:
        if item.claim_id is None:
            raise ClaimCoreError(
                409, "RESOLUTION_BLOCKED_ON_INPUTS", "Decline claim is unavailable"
            )
        claim, _fields, _blocked = self.app.state.claim_service.hydrate_claim(
            item.claim_id, actor, paths=[]
        )
        if claim.status not in DECLINE_APPROVAL_STATUS_VALUES:
            raise ClaimCoreError(
                409,
                "RESOLUTION_BLOCKED_ON_INPUTS",
                "Claim is no longer in a state where the pending decline can commit",
            )

    def _reopen_after_failed_decline(self, review_id: str) -> None:
        with self.sessions.begin() as session:
            item = self._item(session, review_id, lock=True)
            if item.status != "resolved":
                return
            item.status = "open"
            item.resolved_at = None
            item.resolved_by = None
            item.resolution = None
            item.resolution_payload = null()
            item.resolution_schema_version = None

    def resolve(
        self,
        review_id: str,
        *,
        actor: str,
        action: str,
        schema_version: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        with self.sessions() as session:
            item = self._item(session, review_id)
            session.expunge(item)
        if item.status != "open":
            raise ClaimCoreError(409, "ALREADY_RESOLVED", "Review item is no longer open")
        contract = self.contracts.get(item.type)
        code = self.authorizer.resolve_role_code(
            actor=actor,
            contract=contract,
            subtype=item.subtype,
        )
        if code is not None:
            self._deny(item, actor, code)
        code = self.authorizer.resolve_band_code(
            actor=actor,
            contract=contract,
            band_amount=self._band_amount(item, contract.band_amount_path, actor),
        )
        if code is not None:
            self._deny(item, actor, code)
        if action not in contract.resolution_actions:
            raise ClaimCoreError(422, "PAYLOAD_INVALID", "Resolution action is not allowed")
        try:
            self.contracts.validate(item.type, schema_version, payload)
        except KeyError as error:
            raise ClaimCoreError(
                422, "SCHEMA_VERSION_UNKNOWN", "Unknown resolution schema"
            ) from error
        except ValueError as error:
            raise ClaimCoreError(422, "PAYLOAD_INVALID", str(error)) from error
        self._validate_action_payload(action, item.type, payload)
        decline_reason = None
        if (
            item.type == "EXCEPTION"
            and item.subtype == "decline_approval_required"
            and action == "approve"
        ):
            decline_reason = item.payload.get("reason")
            if item.claim_id is None or not isinstance(decline_reason, str) or not decline_reason:
                raise ClaimCoreError(
                    409, "RESOLUTION_BLOCKED_ON_INPUTS", "Decline inputs are unavailable"
                )
            self._validate_decline_claim_state(item, actor)
        self._assert_open_locked(review_id)
        self._side_effect_before(item, action, payload, actor)

        resolution = RESOLUTIONS[action]
        event_payload = {
            **payload,
            "review_id": item.id,
            "type": item.type,
            "schema_version": schema_version,
            "resolution": resolution,
        }
        with self.sessions.begin() as session:
            current = self._item(session, review_id, lock=True)
            if current.status != "open":
                raise ClaimCoreError(409, "ALREADY_RESOLVED", "Review item is no longer open")
            current.status = "resolved"
            current.resolved_at = self.app.state.clock()
            current.resolved_by = actor
            current.resolution = resolution
            current.resolution_payload = dict(payload)
            current.resolution_schema_version = schema_version
            resolved_event = self.app.state.record_event(
                session,
                claim_id=current.claim_id,
                event_type="review.resolved",
                payload=event_payload,
                actor=actor,
                correlation_id=current.id,
            )
            session.flush()
            session.expunge(current)

        if (
            item.type == "EXCEPTION"
            and item.subtype == "decline_approval_required"
            and action == "approve"
        ):
            try:
                self.app.state.claim_service.decline_claim(
                    item.claim_id,
                    decline_reason,
                    actor,
                    approved_by_event=resolved_event.id,
                )
            except Exception as error:  # noqa: BLE001 - compensate every failed effect
                self._reopen_after_failed_decline(review_id)
                raise ClaimCoreError(
                    409,
                    "RESOLUTION_BLOCKED_ON_INPUTS",
                    "Decline did not commit; the review item was reopened",
                ) from error
        return self.get_item(review_id, actor=actor)


__all__ = ["ReviewService"]
