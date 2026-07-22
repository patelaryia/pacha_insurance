"""Review queue reads and the versioned, human-authorised resolution engine."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import column, null, select, table, text
from sqlalchemy.orm import Session, sessionmaker

from claim_core import ClaimCoreError, FieldDefinition, FieldWrite, field_dictionary
from cop_runtime.templates import TemplateRenderBlocked
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
BAND_QUEUE_TYPES = frozenset({"PACK_REVIEW", "EX_GRATIA", "DRAFT_RELEASE"})


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
        self._resolution_validators: dict[
            str, Callable[[ReviewItem, str, dict[str, Any], str], None]
        ] = {}

    def register_resolution_validator(
        self,
        type_name: str,
        validator: Callable[[ReviewItem, str, dict[str, Any], str], None],
    ) -> None:
        """Install one package-owned fail-closed validator for a review type."""

        if type_name in self._resolution_validators:
            raise ValueError(f"resolution validator {type_name!r} is already registered")
        if not callable(validator):
            raise ValueError(f"resolution validator {type_name!r} must be callable")
        self._resolution_validators[type_name] = validator

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

    @staticmethod
    def _field_definition(item: ReviewItem) -> FieldDefinition | None:
        path = item.payload.get("path")
        if not isinstance(path, str) or not path:
            return None
        return field_dictionary().get(path)

    @staticmethod
    def _candidate_blob_key(item: ReviewItem) -> str | None:
        document_id = item.payload.get("document_id")
        blob_key = item.payload.get("candidate_blob_ref")
        if not isinstance(document_id, str) or not document_id:
            return None
        prefix = f"review-candidates/{document_id}/"
        if (
            not isinstance(blob_key, str)
            or not blob_key.startswith(prefix)
            or "/" in blob_key[len(prefix) :]
            or blob_key.endswith("/")
        ):
            return None
        return blob_key

    def _candidate_value(self, item: ReviewItem) -> Any:
        candidate = item.payload.get("candidate_value")
        if candidate != "__redacted__":
            if candidate is None:
                raise ClaimCoreError(
                    409,
                    "RESOLUTION_BLOCKED_ON_INPUTS",
                    "Review candidate is unavailable",
                )
            return candidate
        blob_key = self._candidate_blob_key(item)
        if blob_key is None:
            raise ClaimCoreError(
                409,
                "RESOLUTION_BLOCKED_ON_INPUTS",
                "Private review candidate reference is invalid",
            )
        try:
            return json.loads(self.app.state.blob_store.get(blob_key))
        except (OSError, KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ClaimCoreError(
                409,
                "RESOLUTION_BLOCKED_ON_INPUTS",
                "Private review candidate is unavailable",
            ) from error

    def _transport_payload(self, item: ReviewItem) -> dict[str, Any]:
        payload = dict(item.payload)
        if item.type != "FIELD_VERIFY":
            return payload
        definition = self._field_definition(item)
        if definition is not None:
            payload["value_type"] = definition.value_type
            if definition.enum_values is not None:
                payload["allowed_values"] = sorted(definition.enum_values)
        capability = payload.get("capability_id")
        if not isinstance(capability, str) or not capability.strip():
            engine = getattr(self.app.state, "doc_intel", None)
            configured = getattr(engine, "review_capability_id", None)
            if isinstance(payload.get("document_id"), str) and isinstance(configured, str):
                payload["capability_id"] = configured
        if payload.get("candidate_value") == "__redacted__":
            try:
                payload["candidate_value"] = self._candidate_value(item)
                payload["candidate_status"] = "available"
            except ClaimCoreError:
                payload["candidate_value"] = None
                payload["candidate_status"] = "blocked_on_inputs"
        payload.pop("candidate_blob_ref", None)
        return payload

    def serialise(self, item: ReviewItem) -> dict[str, Any]:
        contract = self.contracts.get(item.type, item.subtype)
        return {
            "id": item.id,
            "claim_id": item.claim_id,
            "type": item.type,
            "subtype": item.subtype,
            "status": item.status,
            "assigned_to": item.assigned_to,
            "payload": self._transport_payload(item),
            "source_event_id": item.source_event_id,
            "created_at": self._iso(item.created_at),
            "resolved_at": self._iso(item.resolved_at),
            "resolved_by": item.resolved_by,
            "resolution": item.resolution,
            "resolution_payload": item.resolution_payload,
            "resolution_schema_version": item.resolution_schema_version,
            "workspace_layout": contract.workspace_layout,
            "resolution_schema": contract.resolution_schema,
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
        role = self._read_role(actor)
        if scope not in {"mine", "pool", "band"}:
            raise ClaimCoreError(
                422, "VALUE_TYPE_MISMATCH", "scope must be mine, pool, or band"
            )
        with self.sessions() as session:
            query = select(ReviewItem)
            if scope == "mine" and role != "auditor":
                query = query.where(
                    ReviewItem.claim_id.in_(
                        select(CLAIMS.c.id).where(CLAIMS.c.assigned_to == actor)
                    )
                )
            elif scope == "band":
                if role not in self.authorizer.bands:
                    return []
                query = query.where(
                    ReviewItem.type.in_(BAND_QUEUE_TYPES), ReviewItem.status == "open"
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
        if scope == "band":
            eligible = []
            for item in items:
                contract = self.contracts.get(item.type, item.subtype)
                if role not in contract.authorised_roles:
                    continue
                amount = self._band_amount(item, contract.band_amount_path, actor)
                if (
                    self.authorizer.resolve_band_code(
                        actor=actor,
                        contract=contract,
                        band_amount=amount,
                    )
                    is None
                ):
                    eligible.append(item)
            items = eligible
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

    def _field_verify(
        self,
        item: ReviewItem,
        action: str,
        payload: dict[str, Any],
        actor: str,
    ) -> None:
        if item.claim_id is None:
            raise ClaimCoreError(409, "RESOLUTION_BLOCKED_ON_INPUTS", "Review has no claim")
        path = item.payload.get("path")
        definition = self._field_definition(item)
        if not isinstance(path, str) or definition is None:
            raise ClaimCoreError(
                409,
                "RESOLUTION_BLOCKED_ON_INPUTS",
                "Reviewed field contract is unavailable",
            )
        if action == "approve":
            value = self._candidate_value(item)
        else:
            corrected = payload["corrected_fields"]
            if set(corrected) != {path}:
                raise ClaimCoreError(
                    422,
                    "PAYLOAD_INVALID",
                    "FIELD_VERIFY correction must target only the reviewed field",
                )
            value = corrected[path]
        source_ref: dict[str, Any] = {"user_id": actor, "review_item_id": item.id}
        document_id = item.payload.get("document_id")
        page = item.payload.get("page")
        citation = item.payload.get("citation")
        bbox = citation.get("bbox") if isinstance(citation, dict) else None
        if (
            isinstance(document_id, str)
            and isinstance(page, int)
            and not isinstance(page, bool)
            and page > 0
            and isinstance(bbox, list)
            and len(bbox) == 4
        ):
            source_ref.update(
                {
                    "document_id": document_id,
                    "page": page,
                    "bbox": list(bbox),
                    "review_verified": True,
                }
            )
        self.app.state.claim_service.write_fields(
            item.claim_id,
            [
                FieldWrite(
                    path=path,
                    value=value,
                    value_type=definition.value_type,
                    source_type="human",
                    source_ref=source_ref,
                    verification_state="human_verified",
                )
            ],
            actor,
        )

    def _coverage_manual(
        self,
        item: ReviewItem,
        payload: dict[str, Any],
        actor: str,
    ) -> None:
        if item.claim_id is None:
            raise ClaimCoreError(409, "RESOLUTION_BLOCKED_ON_INPUTS", "Review has no claim")
        raw_fields = payload.get("fields")
        if not isinstance(raw_fields, dict):
            raise ClaimCoreError(422, "PAYLOAD_INVALID", "Coverage fields are required")
        dictionary = field_dictionary()
        writes = []
        for path, value in raw_fields.items():
            if value in {None, ""}:
                continue
            definition = dictionary.get(path)
            if definition is None:
                raise ClaimCoreError(
                    422,
                    "PAYLOAD_INVALID",
                    f"Coverage path {path!r} is not registered",
                )
            writes.append(
                FieldWrite(
                    path=path,
                    value=value,
                    value_type=definition.value_type,
                    source_type="human",
                    source_ref={"user_id": actor, "review_item_id": item.id},
                    verification_state="human_verified",
                )
            )
        if not writes:
            raise ClaimCoreError(
                409,
                "RESOLUTION_BLOCKED_ON_INPUTS",
                "Coverage review supplied no committed field values",
            )
        self.app.state.claim_service.write_fields(item.claim_id, writes, actor)

    def _execute_staged_action(self, item: ReviewItem) -> None:
        action = item.payload.get("action")
        if not isinstance(action, dict) or action.get("type") != "intake.create_claim":
            return
        action_payload = action.get("payload")
        if not isinstance(action_payload, dict):
            raise ClaimCoreError(
                409,
                "RESOLUTION_BLOCKED_ON_INPUTS",
                "Staged claim-creation payload is unavailable",
            )
        runtime = getattr(self.app.state, "agent_runtime", None)
        if runtime is None:
            raise ClaimCoreError(
                409,
                "RESOLUTION_BLOCKED_ON_INPUTS",
                "Agent runtime is unavailable",
            )
        from agent_runtime import Action

        try:
            runtime.execute_staged(Action(type="intake.create_claim", payload=action_payload))
        except Exception as error:  # noqa: BLE001 - keep the staged item open
            raise ClaimCoreError(
                409,
                "RESOLUTION_BLOCKED_ON_INPUTS",
                "Claim creation did not commit; the review item remains open",
            ) from error

    def _validate_decline_release(self, item: ReviewItem, actor: str) -> list[str]:
        if item.claim_id is None:
            raise ClaimCoreError(
                409, "RESOLUTION_BLOCKED_ON_INPUTS", "Decline claim is unavailable"
            )
        claim, _fields, _blocked = self.app.state.claim_service.hydrate_claim(
            item.claim_id, actor, paths=[]
        )
        if claim.status != "TRIAGED":
            raise ClaimCoreError(
                409,
                "RESOLUTION_BLOCKED_ON_INPUTS",
                "Decline release requires a TRIAGED claim",
            )
        pack_id, separator, version = claim.pack_version.partition("@")
        if not separator:
            raise ClaimCoreError(
                409, "RESOLUTION_BLOCKED_ON_INPUTS", "Claim pack pin is malformed"
            )
        definition = self.app.state.cop_runtime.template_registry(pack_id, version).get("T-07")
        if definition.status == "pending_capture":
            raise ClaimCoreError(
                409,
                "RESOLUTION_BLOCKED_ON_INPUTS",
                "T-07 is pending capture",
            )
        try:
            self.app.state.cop_runtime.render("T-07", item.claim_id, actor)
        except TemplateRenderBlocked as error:
            raise ClaimCoreError(
                409,
                "RESOLUTION_BLOCKED_ON_INPUTS",
                "T-07 cannot render from the captured claim inputs",
                extra={
                    "reason": error.reason,
                    "missing_fields": list(error.missing_fields),
                    "under_verified": list(error.under_verified),
                },
            ) from error
        with self.app.state.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, email, meta FROM parties WHERE claim_id = :claim_id "
                    "ORDER BY id"
                ),
                {"claim_id": item.claim_id},
            ).all()
        recipients = []
        for party_id, email, raw_meta in rows:
            meta = json.loads(raw_meta) if isinstance(raw_meta, str) else raw_meta
            if (
                isinstance(meta, dict)
                and meta.get("source") == "intimation_sender"
                and isinstance(email, str)
                and email.strip()
            ):
                recipients.append(str(party_id))
        if len(recipients) != 1:
            raise ClaimCoreError(
                409,
                "RESOLUTION_BLOCKED_ON_INPUTS",
                "Decline release requires exactly one captured intimation sender",
            )
        return recipients

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
        if item.type == "FIELD_VERIFY" and action in {"approve", "edit_approve"}:
            if item.subtype == "coverage_manual":
                self._coverage_manual(item, payload, actor)
            else:
                self._field_verify(item, action, payload, actor)
        elif item.type == "DOC_SPLIT" and action == "edit_approve":
            self._doc_split(item, payload, actor)
        elif item.type == "DRAFT_RELEASE" and action == "approve":
            if item.subtype != "decline_draft":
                self._execute_staged_action(item)

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

    def cancel(self, review_id: str, *, actor: str, reason: str) -> dict[str, Any]:
        """Withdraw one open item that a newer producer version superseded.

        This is not a human decision: it records no resolution and grants no
        approval. Only the package that produced the item may withdraw it.
        """

        if not isinstance(reason, str) or not reason.strip():
            raise ClaimCoreError(422, "PAYLOAD_INVALID", "Cancellation requires a reason")
        with self.sessions.begin() as session:
            item = self._item(session, review_id, lock=True)
            if item.status != "open":
                raise ClaimCoreError(409, "ALREADY_RESOLVED", "Review item is no longer open")
            item.status = "cancelled"
            item.resolved_at = self.app.state.clock()
            claim_id = item.claim_id
            item_type = item.type
            subtype = item.subtype
            self.app.state.record_event(
                session,
                claim_id=claim_id,
                event_type="review.cancelled",
                payload={
                    "review_id": review_id,
                    "type": item_type,
                    "subtype": subtype,
                    "reason": reason,
                },
                actor=actor,
                correlation_id=review_id,
            )
        return {"id": review_id, "status": "cancelled"}

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
        contract = self.contracts.get(item.type, item.subtype)
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
            self.contracts.validate(
                item.type,
                schema_version,
                payload,
                subtype=item.subtype,
            )
        except KeyError as error:
            raise ClaimCoreError(
                422, "SCHEMA_VERSION_UNKNOWN", "Unknown resolution schema"
            ) from error
        except ValueError as error:
            raise ClaimCoreError(422, "PAYLOAD_INVALID", str(error)) from error
        self._validate_action_payload(action, item.type, payload)
        validator = self._resolution_validators.get(item.type)
        if validator is not None:
            validator(item, action, payload, actor)
        decline_reason = None
        decline_release_recipients: list[str] | None = None
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
        if (
            item.type == "DRAFT_RELEASE"
            and item.subtype == "decline_draft"
            and action == "approve"
        ):
            decline_release_recipients = self._validate_decline_release(item, actor)
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
        if decline_release_recipients is not None:
            try:
                self.app.state.claim_service.decline_claim(
                    item.claim_id,
                    "below_excess",
                    actor,
                )
            except Exception as error:  # noqa: BLE001 - compensate every failed effect
                self._reopen_after_failed_decline(review_id)
                raise ClaimCoreError(
                    409,
                    "RESOLUTION_BLOCKED_ON_INPUTS",
                    "Decline did not commit; the review item was reopened",
                ) from error
            self.app.state.agent_runtime.comms.send(
                template_id="T-07",
                claim_id=item.claim_id,
                to_party_ids=decline_release_recipients,
                attachments=(),
                capability_id="triage.decline_draft",
                actor=actor,
            )
        return self.get_item(review_id, actor=actor)


__all__ = ["ReviewService"]
