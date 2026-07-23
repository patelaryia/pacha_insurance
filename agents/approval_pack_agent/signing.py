"""Human-only signing, deterministic authority routing, and the S-3 loop.

Nothing in this module is an autonomy capability. `pack.note_draft` stays max
L3 for draft production only: no code path here gates, stages, or auto-executes
a signature, and no funds-transfer or settlement operation exists.

The crash-safe contract (register #247) is: prepare the exact graded candidate
and its immutable artifact *before* `review.resolved`, then finalise from that
durable event, resuming from whichever of `pack.note_sign_prepared`,
`pack.note_signed` and `pack.routed` already landed.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import text

from approval_pack_agent.conversion import (
    ConversionFailed,
    is_pdf,
    sha256_hex,
    stamped_html,
)
from approval_pack_agent.models import NoteDraft
from approval_pack_agent.note import NoteInputsInvalid, canonical_body_hash
from approval_pack_agent.resolver import ACTOR, _json
from claim_core import ClaimCoreError, new_ulid
from cop_runtime.templates import TemplateRenderBlocked

# Authority-matrix side effects are pack data in the exact PRD-02 §2.5 form.
# An unrecognised verb is blocked, never ignored: it may be the alert that a
# >KES 4M approval is required to carry.
RENDER_SIDE_EFFECT = re.compile(r"^render\s+(?P<template_id>T-[0-9]+[a-z]?)$")
SIGN_ACTIONS = frozenset({"approve", "edit_approve"})
APPROVAL_SUBTYPE = "approval_pack"
NOTE_SUBTYPE = "approval_note"
ROUTE_SNAPSHOT_KEYS = (
    "draft_id",
    "body_sha256",
    "merged_event_id",
    "note_signed_event_id",
    "routing_amount_cents",
    "required_role",
)


class RoutingBlocked(ClaimCoreError):
    """The routing input or a mandated side effect is not captured."""

    def __init__(self, blocked_on: str, detail: str) -> None:
        super().__init__(
            409,
            "ROUTING_BLOCKED_ON_INPUTS",
            detail,
            extra={"blocked_on": blocked_on},
        )


class SigningService:
    """Own sign preparation, the idempotent finaliser, and the approval loop."""

    def __init__(self, service: Any) -> None:
        self.service = service
        self.app = service.app
        self.config = service.config
        self.notes = service.notes
        self.workspace = service.workspace

    @contextmanager
    def resolution_scope(
        self, item: Any, action: str, payload: dict[str, Any], actor: str
    ) -> Iterator[None]:
        """Hold the claim guard through validation and durable resolution."""

        del action, payload, actor
        if item.subtype != NOTE_SUBTYPE or not isinstance(item.claim_id, str):
            yield
            return
        with self.service._claim_guard(item.claim_id, row_lock=False):
            yield

    # -- routing ---------------------------------------------------------------

    def _pack_pin(self, claim_id: str, actor: str) -> tuple[str, str]:
        claim, _fields, _blocked = self.app.state.claim_service.hydrate_claim(
            claim_id, actor, paths=[]
        )
        pack_id, separator, version = claim.pack_version.partition("@")
        if not separator:
            raise RoutingBlocked("claim.pack_version", "Claim pack pin is malformed")
        return pack_id, version

    def _latest_calc_run(self, claim_id: str, calc_id: str) -> dict[str, Any] | None:
        rows = self.service._rows(
            "SELECT id, version, output FROM calc_runs WHERE claim_id = :claim_id "
            "AND calc_id = :calc_id AND status = 'executed' ORDER BY ts, id",
            claim_id=claim_id,
            calc_id=calc_id,
        )
        return rows[-1] if rows else None

    def routing_input(self, claim_id: str, actor: str) -> dict[str, Any]:
        """Return the routed amount with the exact provenance it came from.

        PRD-02 §2.5 binds the amount to C-08 when it is live and to the
        `reserve.total` fallback while it is not. The fallback only routes
        upward, which is the safe failure mode; neither branch is guessed.
        """

        pack_id, version = self._pack_pin(claim_id, actor)
        runtime = self.app.state.cop_runtime
        contract = runtime.routing_contract(pack_id, version)
        amount = runtime.routing_amount(claim_id, actor)
        if amount is None:
            raise RoutingBlocked(
                str(contract["fallback_path"] or contract["calc_id"] or "routing_amount"),
                "No committed routing amount is available for this claim",
            )
        if contract["calc_status"] == "live":
            run = self._latest_calc_run(claim_id, str(contract["calc_id"]))
            if run is None:
                raise RoutingBlocked(
                    str(contract["calc_id"]),
                    "The routing calculation produced no reconstructable run",
                )
            provenance: dict[str, Any] = {
                "source": "calc",
                "calc_id": contract["calc_id"],
                "calc_version": run["version"],
                "calc_run_id": run["id"],
            }
        else:
            path = contract["fallback_path"]
            _claim, fields, _blocked = self.app.state.claim_service.hydrate_claim(
                claim_id, actor, paths=[str(path)]
            )
            field = fields.get(str(path))
            if field is None:
                raise RoutingBlocked(str(path), "The binding routing fallback is absent")
            provenance = {
                "source": "claim_field",
                "path": path,
                "field_id": field.id,
                "field_version": field.version,
                "blocked_calc_id": contract["calc_id"],
                "blocked_calc_status": contract["calc_status"],
            }
        route = runtime.authority_matrix(pack_id, version).route(amount)
        return {
            "amount_cents": int(amount),
            "required_role": route.role,
            "side_effects": list(route.side_effects),
            "provenance": provenance,
            "pack_id": pack_id,
            "pack_version": version,
        }

    def side_effect_templates(self, snapshot: dict[str, Any]) -> list[str]:
        """Resolve declared side effects to template ids, refusing the unknown."""

        templates: list[str] = []
        for side_effect in snapshot["side_effects"]:
            match = RENDER_SIDE_EFFECT.fullmatch(str(side_effect).strip())
            if match is None:
                raise RoutingBlocked(
                    "authority_matrix.side_effects",
                    f"Authority side effect {side_effect!r} has no captured contract",
                )
            templates.append(match.group("template_id"))
        return templates

    def assert_side_effects_renderable(
        self, claim_id: str, actor: str, snapshot: dict[str, Any]
    ) -> list[str]:
        """Refuse to sign while a mandated alert template is uncaptured (§6)."""

        pack_id, version = self._pack_pin(claim_id, actor)
        registry = self.app.state.cop_runtime.template_registry(pack_id, version)
        templates = self.side_effect_templates(snapshot)
        for template_id in templates:
            definition = registry.get(template_id)
            if definition.status != "live":
                raise RoutingBlocked(
                    "open-item-6",
                    f"{template_id} is {definition.status}; the >KES 4M alert cannot render",
                )
        return templates

    # -- sign preparation ------------------------------------------------------

    def _prepared_event(
        self, claim_id: str, draft_id: str, body_sha256: str
    ) -> dict[str, Any] | None:
        for event in self.service._events(claim_id, "pack.note_sign_prepared"):
            payload = event["payload"]
            if (
                payload.get("note_draft_id") == draft_id
                and payload.get("body_sha256") == body_sha256
            ):
                return event
        return None

    def _store(self, key: str, content: bytes) -> None:
        try:
            self.service.store.put_immutable(
                key, content, retention=self.config.retention
            )
        except ConversionFailed as error:
            # PRD-09 invariant: an uncertain immutable write is never retried
            # blind. The store already compared the expected content hash.
            self.notes.refuse(
                self.service_claim_id(key),
                subtype="uncertain_write",
                facts={"blob_key": key, "detail": error.detail},
                risk="a second differing approval artifact must never be written",
                recommendation="inspect the immutable store before retrying the signature",
                correlation_id=None,
            )
            raise ClaimCoreError(
                409,
                "UNCERTAIN_WRITE",
                "The immutable artifact store holds different bytes for this key",
            ) from error

    @staticmethod
    def service_claim_id(blob_key: str) -> str:
        return blob_key.split("/")[1]

    def prepare_signature(
        self, review: dict[str, Any], payload: dict[str, Any], actor: str
    ) -> dict[str, Any]:
        """Grade and immutably render the exact candidate the officer signed."""

        claim_id = str(review["claim_id"])
        claim, _fields, _blocked = self.app.state.claim_service.hydrate_claim(
            claim_id, actor, paths=[]
        )
        if claim.status != "PACK_READY":
            raise ClaimCoreError(
                409,
                "SIGN_BLOCKED_ON_STATE",
                "An approval note may only be signed while the claim is PACK_READY",
            )
        root_draft_id = self.workspace.root_for_review(review)
        # 1. Re-read and lock the latest lineage version under the claim guard.
        with self.service._claim_guard(claim_id, row_lock=False):
            draft = self.workspace.current(claim_id, root_draft_id)
            body = draft["body"]
            body_sha256 = canonical_body_hash(body)
            # 2. A stale id or hash never signs.
            if payload.get("draft_id") != draft["id"] or (
                payload.get("body_sha256") != body_sha256
            ):
                raise ClaimCoreError(
                    409,
                    "STALE_NOTE_DRAFT",
                    "The signed version is not the latest saved version",
                    extra={
                        "current_draft_id": draft["id"],
                        "current_version": draft["version"],
                        "current_body_sha256": body_sha256,
                    },
                )
            if draft["status"] == "signed":
                raise ClaimCoreError(
                    409, "NOTE_ALREADY_SIGNED", "This approval note is already signed"
                )
            existing = self._prepared_event(claim_id, draft["id"], body_sha256)
            if existing is not None:
                return {**existing["payload"], "prepared_event_id": existing["id"]}
            # 3. Recompute blockers: C-08 and every uncaptured slot refuse here.
            blockers, readiness = self.workspace.blockers(claim_id, actor)
            if blockers:
                raise ClaimCoreError(
                    409,
                    "SIGN_BLOCKED_ON_INPUTS",
                    "The approval note has unresolved blockers and cannot be signed",
                    extra={"blockers": blockers},
                )
            # The route is decided before any artifact exists, so a >KES 4M claim
            # whose T-03 alert is uncaptured creates nothing at all (§6).
            snapshot = self.routing_input(claim_id, actor)
            self.assert_side_effects_renderable(claim_id, actor, snapshot)
            # 4. Grade the exact final candidate bytes.
            try:
                html = self.notes.render_body(
                    claim_id=claim_id,
                    actor=actor,
                    readiness=readiness,
                    body=body,
                    blockers=[],
                    signable=True,
                )
            except NoteInputsInvalid as error:
                raise ClaimCoreError(
                    409, "SIGN_BLOCKED_ON_INPUTS", f"The final note refused to render: {error}"
                ) from error
            html_key = (
                f"approval-packs/{claim_id}/notes/signed/{sha256_hex(html)}.html"
            )
            self._store(html_key, html)
            render_event_id = self.service._emit(
                claim_id=claim_id,
                event_type="template.rendered",
                payload={
                    "template_id": "T-01",
                    "template_version": body["template_version"],
                    "channel": "pdf",
                    "note_draft_id": draft["id"],
                    "note_version": draft["version"],
                    "body_sha256": body_sha256,
                    "blob_key": html_key,
                    "signable": True,
                },
                correlation_id=review["id"],
                actor=actor,
            )
            integrity = self._grade(claim_id, draft, html_key, render_event_id)
            if integrity["g_tpl_result"] != "pass" or integrity["g_note_result"] != "pass":
                self.notes.refuse(
                    claim_id,
                    subtype="note_integrity_failed",
                    facts={
                        "note_draft_id": draft["id"],
                        "note_version": draft["version"],
                        "body_sha256": body_sha256,
                        "integrity": integrity,
                    },
                    risk="a known-wrong approval note must never carry a human signature",
                    recommendation="inspect the failed grader run and regenerate the note",
                    correlation_id=review["id"],
                )
                raise ClaimCoreError(
                    409,
                    "SIGN_INTEGRITY_FAILED",
                    "An integrity gate failed on the exact candidate; the review stays open",
                    extra={"integrity": integrity},
                )
            # 5. Render the final network-disabled PDF under the pinned policy.
            pdf = self._render_pdf(html)
            pdf_key = f"approval-packs/{claim_id}/notes/signed/{sha256_hex(pdf)}.pdf"
            self._store(pdf_key, pdf)
            # 6. Record ids, hashes, artifact ref, grader refs and the route input.
            prepared_payload = {
                "note_draft_id": draft["id"],
                "note_version": draft["version"],
                "body_sha256": body_sha256,
                "root_draft_id": root_draft_id,
                "review_id": review["id"],
                "merged_pack_event_id": body["merged_pack"]["event_id"],
                "merged_pack_sha256": body["merged_pack"]["sha256"],
                "artifact_blob_key": pdf_key,
                "artifact_sha256": sha256_hex(pdf),
                "artifact_html_blob_key": html_key,
                "render_event_id": render_event_id,
                "object_lock_status": self.config.object_lock_status,
                "integrity": integrity,
                "route_input": snapshot,
                "actor": actor,
            }
            prepared_event_id = self.service._emit(
                claim_id=claim_id,
                event_type="pack.note_sign_prepared",
                payload=prepared_payload,
                correlation_id=review["id"],
                actor=actor,
            )
        return {**prepared_payload, "prepared_event_id": prepared_event_id}

    def _grade(
        self,
        claim_id: str,
        draft: dict[str, Any],
        blob_key: str,
        render_event_id: str,
    ) -> dict[str, Any]:
        subject = {
            "claim_id": claim_id,
            "template_id": "T-01",
            "blob_key": blob_key,
            "signable": True,
            "source_event_id": render_event_id,
            "capability_id": "pack.note_draft",
            "note_draft_id": draft["id"],
        }
        tpl = self.app.state.eval_harness.grade("G-TPL", subject, actor="agent:eval")
        note = self.app.state.eval_harness.grade("G-NOTE", subject, actor="agent:eval")
        return {
            "g_tpl_run_id": tpl.grader_run_id,
            "g_note_run_id": note.grader_run_id,
            "g_tpl_result": tpl.result,
            "g_note_result": note.result,
        }

    def _render_pdf(self, html: bytes) -> bytes:
        policy = self.config.render_policy
        stamped = stamped_html(
            html.decode("utf-8"), policy=policy, rendered_at=self.app.state.clock()
        )
        try:
            result = self.service.merge.renderer.render(stamped, policy=policy)
        except (TimeoutError, RuntimeError, OSError) as error:
            # No plaintext fallback exists for a signed note: an approval that
            # does not match the graded artifact must not be produced.
            raise ClaimCoreError(
                409,
                "SIGN_RENDER_FAILED",
                "The final approval note could not be rendered offline",
            ) from error
        content = getattr(result, "pdf_bytes", None)
        if not isinstance(content, bytes) or not is_pdf(content):
            raise ClaimCoreError(
                409, "SIGN_RENDER_FAILED", "The renderer returned no parseable PDF"
            )
        return content

    # -- NOTE_REVIEW resolution guard -----------------------------------------

    def guard_note_review(
        self, item: Any, action: str, payload: dict[str, Any], actor: str
    ) -> None:
        """Refuse or fully prepare a note resolution before it is recorded."""

        if item.subtype != NOTE_SUBTYPE:
            return
        review = self.workspace.review(item.id)
        if action == "reject":
            # Reject signs nothing. The finaliser returns the retained version
            # to `draft` and keeps the claim PACK_READY (#249).
            return
        if action not in SIGN_ACTIONS:
            raise ClaimCoreError(422, "PAYLOAD_INVALID", "Resolution action is not allowed")
        role = self.app.state.review_queue.service.authorizer.role(actor)
        if role not in {"claims_officer", "claims_manager"}:
            raise ClaimCoreError(
                403, "FORBIDDEN_ROLE", "Only the claim officer or manager signs a note"
            )
        self.prepare_signature(review, payload, actor)

    # -- NOTE_REVIEW finalisation ---------------------------------------------

    def _review_row(self, review_id: str) -> dict[str, Any] | None:
        rows = self.service._rows(
            "SELECT id, claim_id, type, subtype, payload FROM review_items "
            "WHERE id = :review_id",
            review_id=review_id,
        )
        if not rows:
            return None
        return {**rows[0], "payload": _json(rows[0]["payload"]) or {}}

    def _correlated(
        self, claim_id: str, event_type: str, correlation_id: str
    ) -> dict[str, Any] | None:
        for event in self.service._events(claim_id, event_type):
            if event["correlation_id"] == correlation_id:
                return event
        return None

    def finalise_note_review(self, event: Any, payload: dict[str, Any]) -> None:
        """Idempotently finalise one durable NOTE_REVIEW resolution."""

        review_id = payload.get("review_id")
        if not isinstance(review_id, str):
            return
        review = self._review_row(review_id)
        if review is None or review["subtype"] != NOTE_SUBTYPE:
            return
        claim_id = str(review["claim_id"])
        if payload.get("resolution") == "rejected":
            self._finalise_rejection(claim_id, review, event)
            return
        self._finalise_signature(claim_id, review, event)

    def _finalise_rejection(
        self, claim_id: str, review: dict[str, Any], event: Any
    ) -> None:
        if self._correlated(claim_id, "pack.note_review_rejected", review["id"]):
            return
        root_draft_id = self.workspace.root_for_review(review)
        draft = self.workspace.current(claim_id, root_draft_id)
        if draft["status"] == "signed":
            return
        with self.service.sessions.begin() as session:
            session.execute(
                text(
                    "UPDATE note_drafts SET status = 'draft' "
                    "WHERE id = :draft_id AND status = 'in_review'"
                ),
                {"draft_id": draft["id"]},
            )
            self.service._record(
                session,
                claim_id=claim_id,
                event_type="pack.note_review_rejected",
                payload={
                    "review_id": review["id"],
                    "note_draft_id": draft["id"],
                    "note_version": draft["version"],
                    "body_sha256": canonical_body_hash(draft["body"]),
                    "retained_status": "draft",
                    "actor": event.actor,
                },
                correlation_id=review["id"],
                actor=event.actor,
            )

    def _finalise_signature(
        self, claim_id: str, review: dict[str, Any], event: Any
    ) -> None:
        with self.service._claim_guard(claim_id, row_lock=False):
            prepared = self._correlated(
                claim_id, "pack.note_sign_prepared", review["id"]
            )
            if prepared is None:
                # Nothing was prepared, so nothing may be signed. The absence is
                # visible in the workspace as `unsigned`; no artifact is invented.
                return
            snapshot = {
                **prepared["payload"],
                "prepared_event_id": prepared["id"],
            }
            draft_id = str(snapshot["note_draft_id"])
            signed_event = self.workspace._signed(claim_id, draft_id)
            if signed_event is None:
                signed_event_id = self._sign(claim_id, snapshot, event)
            else:
                signed_event_id = signed_event["id"]
            if self._correlated(claim_id, "pack.routed", signed_event_id) is None:
                self._route(claim_id, snapshot, signed_event_id, event)
            claim, _fields, _blocked = self.app.state.claim_service.hydrate_claim(
                claim_id, ACTOR, paths=[]
            )
            if claim.status == "PACK_READY":
                self.app.state.claim_service.transition_claim(
                    claim_id,
                    "IN_APPROVAL",
                    {
                        "note_draft_id": draft_id,
                        "note_signed_event_id": signed_event_id,
                    },
                    event.actor,
                )

    def _sign(self, claim_id: str, snapshot: dict[str, Any], event: Any) -> str:
        signed_at = self.app.state.clock()
        with self.service.sessions.begin() as session:
            session.execute(
                text(
                    "UPDATE note_drafts SET status = 'signed', signed_by = :actor, "
                    "signed_at = :signed_at WHERE id = :draft_id AND status != 'signed'"
                ),
                {
                    "actor": event.actor,
                    "signed_at": signed_at,
                    "draft_id": snapshot["note_draft_id"],
                },
            )
            return self.service._record(
                session,
                claim_id=claim_id,
                event_type="pack.note_signed",
                payload={
                    "note_draft_id": snapshot["note_draft_id"],
                    "note_version": snapshot["note_version"],
                    "body_sha256": snapshot["body_sha256"],
                    "review_id": snapshot["review_id"],
                    "merged_pack_event_id": snapshot["merged_pack_event_id"],
                    "merged_pack_sha256": snapshot["merged_pack_sha256"],
                    "artifact_blob_key": snapshot["artifact_blob_key"],
                    "artifact_sha256": snapshot["artifact_sha256"],
                    "prepared_event_id": prepared_id(snapshot),
                    "object_lock_status": snapshot["object_lock_status"],
                    "integrity": snapshot["integrity"],
                    "signed_by": event.actor,
                },
                correlation_id=snapshot["review_id"],
                actor=event.actor,
            )

    def _route(
        self,
        claim_id: str,
        snapshot: dict[str, Any],
        signed_event_id: str,
        event: Any,
    ) -> None:
        route = snapshot["route_input"]
        # R-12: the alert is rendered before the approval item exists, so a
        # required side effect can never be skipped by a later failure (§6).
        rendered: list[dict[str, Any]] = []
        for template_id in self.side_effect_templates(route):
            try:
                result = self.app.state.cop_runtime.render(template_id, claim_id, ACTOR)
            except (TemplateRenderBlocked, LookupError) as error:
                # Preflight already refused an uncaptured template, so reaching
                # here means the pack changed underneath a durable resolution.
                # Refuse visibly and leave the approval unrouted.
                self.notes.refuse(
                    claim_id,
                    subtype="pack_route_side_effect_blocked",
                    facts={
                        "template_id": template_id,
                        "detail": str(error),
                        "note_signed_event_id": signed_event_id,
                    },
                    risk="a mandated >KES 4M alert would be skipped by the approval route",
                    recommendation="capture the alert template, then re-route the signed pack",
                    correlation_id=signed_event_id,
                )
                return
            rendered.append(
                {
                    "template_id": template_id,
                    "template_version": result.template_version,
                    "blob_key": result.blob_key,
                }
            )
        with self.service.sessions.begin() as session:
            review_event_id = self.service._record(
                session,
                claim_id=claim_id,
                event_type="review.created",
                payload={
                    "type": "PACK_REVIEW",
                    "subtype": APPROVAL_SUBTYPE,
                    "capability_id": "pack.route",
                    "draft_id": snapshot["note_draft_id"],
                    "note_version": snapshot["note_version"],
                    "body_sha256": snapshot["body_sha256"],
                    "merged_event_id": snapshot["merged_pack_event_id"],
                    "merged_pack_sha256": snapshot["merged_pack_sha256"],
                    "note_signed_event_id": signed_event_id,
                    "note_signed_sha256": snapshot["artifact_sha256"],
                    "routing_amount_cents": route["amount_cents"],
                    "required_role": route["required_role"],
                    "route_provenance": route["provenance"],
                    "side_effects": rendered,
                    "facts": {
                        "routing_amount_cents": route["amount_cents"],
                        "required_role": route["required_role"],
                    },
                    "risk": "an approved pack commits the insurer to the routed amount",
                    "recommendation": (
                        "read the merged pack and the signed note before approving"
                    ),
                    "resolution_schema": "PACK_REVIEW@2",
                },
                correlation_id=signed_event_id,
                actor=event.actor,
            )
            self.service._record(
                session,
                claim_id=claim_id,
                event_type="pack.routed",
                payload={
                    "note_signed_event_id": signed_event_id,
                    "note_draft_id": snapshot["note_draft_id"],
                    "review_event_id": review_event_id,
                    "routing_amount_cents": route["amount_cents"],
                    "required_role": route["required_role"],
                    "route_provenance": route["provenance"],
                    "side_effects": rendered,
                    "pack_version": route["pack_version"],
                },
                correlation_id=signed_event_id,
                actor=event.actor,
            )

    # -- PACK_REVIEW guard and finalisation -----------------------------------

    def guard_pack_review(
        self, item: Any, action: str, payload: dict[str, Any], actor: str
    ) -> None:
        """Refuse a stale, mismatched, or unannotated approval resolution."""

        if item.subtype != APPROVAL_SUBTYPE:
            return
        claim_id = str(item.claim_id)
        for key in ROUTE_SNAPSHOT_KEYS:
            if payload.get(key) != item.payload.get(key):
                raise ClaimCoreError(
                    422,
                    "PAYLOAD_INVALID",
                    f"Approval payload {key!r} does not match the routed snapshot",
                )
        claim, _fields, _blocked = self.app.state.claim_service.hydrate_claim(
            claim_id, actor, paths=[]
        )
        if claim.status != "IN_APPROVAL":
            raise ClaimCoreError(
                409,
                "RESOLUTION_BLOCKED_ON_INPUTS",
                "The claim is no longer awaiting approval",
            )
        # #248: recompute the routing input. A changed value keeps the item open
        # and requires a fresh route rather than approving a stale figure.
        current = self.routing_input(claim_id, actor)
        recorded = item.payload.get("route_provenance")
        if current["amount_cents"] != item.payload.get("routing_amount_cents") or (
            current["provenance"] != recorded
        ):
            raise ClaimCoreError(
                409,
                "APPROVAL_ROUTE_STALE",
                "The routing input changed after this pack was routed",
                extra={
                    "routed_amount_cents": item.payload.get("routing_amount_cents"),
                    "current_amount_cents": current["amount_cents"],
                    "current_required_role": current["required_role"],
                },
            )
        if action == "edit_approve":
            annotation = payload.get("annotation")
            if not isinstance(annotation, str) or not annotation.strip():
                raise ClaimCoreError(
                    422,
                    "PAYLOAD_INVALID",
                    "Annotate & Approve requires a non-empty manager annotation",
                )
        if action == "reject":
            reasons = payload.get("reasons")
            if not isinstance(reasons, list) or not reasons:
                raise ClaimCoreError(
                    422, "PAYLOAD_INVALID", "Reject requires structured reasons"
                )
            declared = {
                change.get("path")
                for change in payload.get("diff", {}).get("typed_changes", [])
            }
            for reason in reasons:
                path = reason.get("field_path")
                if path is not None and path not in declared:
                    raise ClaimCoreError(
                        422,
                        "PAYLOAD_INVALID",
                        "A named corrected field path must appear in the typed diff",
                    )

    def finalise_pack_review(self, event: Any, payload: dict[str, Any]) -> None:
        """Idempotently finalise one durable approval resolution."""

        review_id = payload.get("review_id")
        if not isinstance(review_id, str):
            return
        review = self._review_row(review_id)
        if review is None or review["subtype"] != APPROVAL_SUBTYPE:
            return
        claim_id = str(review["claim_id"])
        claim, _fields, _blocked = self.app.state.claim_service.hydrate_claim(
            claim_id, ACTOR, paths=[]
        )
        if payload.get("resolution") in {"approved", "edited"}:
            # Annotation is retained only in the resolution event; no signed
            # artifact is ever mutated (#254).
            if claim.status == "IN_APPROVAL":
                self.app.state.claim_service.transition_claim(
                    claim_id,
                    "APPROVED",
                    {"review_id": review_id},
                    event.actor,
                )
            return
        self._finalise_manager_rejection(claim_id, review, event, payload)

    def _finalise_manager_rejection(
        self,
        claim_id: str,
        review: dict[str, Any],
        event: Any,
        payload: dict[str, Any],
    ) -> None:
        with self.service._claim_guard(claim_id, row_lock=False):
            if self._correlated(claim_id, "review.created", review["id"]):
                return
            claim, _fields, _blocked = self.app.state.claim_service.hydrate_claim(
                claim_id, ACTOR, paths=[]
            )
            reasons = list(payload.get("reasons", []))
            if claim.status == "IN_APPROVAL":
                self.app.state.claim_service.transition_claim(
                    claim_id,
                    "PACK_READY",
                    {
                        "review_id": review["id"],
                        # The FSM owns the binding `{code, detail}` reject shape; the
                        # corrected field path stays with the note revision block.
                        "reasons": [
                            {"code": reason["code"], "detail": reason["detail"]}
                            for reason in reasons
                        ],
                    },
                    event.actor,
                )
            signed = self.service._rows(
                "SELECT id, version, status, body FROM note_drafts WHERE id = :draft_id",
                draft_id=review["payload"].get("draft_id"),
            )
            if not signed:
                return
            body = _json(signed[0]["body"]) or {}
            rejection = {
                "review_id": review["id"],
                "rejected_by": event.actor,
                "rejected_version": signed[0]["version"],
                "signed_draft_id": signed[0]["id"],
                "note_signed_event_id": review["payload"].get(
                    "note_signed_event_id"
                ),
                "reasons": reasons,
            }
            clone = {
                key: value
                for key, value in body.items()
                if key not in {"lineage", "body_sha256"}
            }
            # The reasons live in their own visible block. They are never spliced
            # into generated commentary (#250), and the signed version is retained.
            clone["manager_rejection"] = rejection
            clone["signable"] = False
            draft_id = new_ulid()
            clone["lineage"] = {
                "root_draft_id": draft_id,
                "parent_draft_id": None,
                "review_id": None,
            }
            clone["body_sha256"] = canonical_body_hash(clone)
            merged_event_id = str(body.get("merged_pack", {}).get("event_id"))
            merged_payload = self._merged_payload(claim_id, body)
            with self.service.sessions.begin() as session:
                version = self.workspace._next_version(session, claim_id)
                session.execute(
                    text(
                        "UPDATE note_drafts SET status = 'superseded' "
                        "WHERE claim_id = :claim_id "
                        "AND status IN ('draft', 'in_review')"
                    ),
                    {"claim_id": claim_id},
                )
                session.add(
                    NoteDraft(
                        id=draft_id,
                        claim_id=claim_id,
                        version=version,
                        body=clone,
                        status="in_review",
                        edited_by=None,
                        signed_by=None,
                        signed_at=None,
                    )
                )
                self.service._record(
                    session,
                    claim_id=claim_id,
                    event_type="review.created",
                    payload=self.notes.note_review_payload(
                        draft_id=draft_id,
                        version=version,
                        body=clone,
                        merged_event_id=merged_event_id,
                        merged_payload=merged_payload,
                        review_artifact_blob_key=str(
                            review["payload"].get("note_signed_event_id") or ""
                        ),
                        manager_rejection=rejection,
                    ),
                    correlation_id=review["id"],
                )

    def _merged_payload(self, claim_id: str, body: dict[str, Any]) -> dict[str, Any]:
        event_id = body.get("merged_pack", {}).get("event_id")
        for event in self.service._events(claim_id, "pack.merged"):
            if event["id"] == event_id:
                return event["payload"]
        raise ClaimCoreError(
            409, "MERGED_PACK_UNAVAILABLE", "The cited merged pack version is absent"
        )


def prepared_id(snapshot: dict[str, Any]) -> str | None:
    """Return the prepared-event id when the caller carried one forward."""

    value = snapshot.get("prepared_event_id")
    return value if isinstance(value, str) else None


__all__ = [
    "APPROVAL_SUBTYPE",
    "NOTE_SUBTYPE",
    "RENDER_SIDE_EFFECT",
    "RoutingBlocked",
    "SigningService",
]
