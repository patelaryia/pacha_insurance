"""The NOTE_REVIEW workspace read, append-version autosave, and artifact reads.

Nothing here signs, routes, or moves the FSM. Every read recomputes the
canonical body hash and the blocker list from durable state, so a client can
never assert that a note is signable (PACKET-19 §2/§3, register #246/#253).
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import text

from approval_pack_agent.config import canonical_json
from approval_pack_agent.models import NoteDraft
from approval_pack_agent.note import (
    NoteInputsInvalid,
    canonical_body_hash,
    numeric_tokens,
)
from approval_pack_agent.resolver import _json
from claim_core import ClaimCoreError, new_ulid

# PRD-08 §8.5: only the officer working the claim, or their manager, edits a note.
EDIT_ROLES = frozenset({"claims_officer", "claims_manager"})
# The NOTE_REVIEW contract's operational roles may open the workspace read-only.
WORKSPACE_ROLES = frozenset(
    {
        "claims_officer",
        "asst_claims_manager",
        "claims_manager",
        "gm",
        "md",
        "chairman",
    }
)
# #253: exactly the two append-only artifact-index event types cross to a browser.
ARTIFACT_EVENT_TYPES = {
    "pack.merged": "blob_key",
    "pack.note_signed": "artifact_blob_key",
}


class NoteWorkspace:
    """Read and edit one approval-note lineage without ever signing it."""

    def __init__(self, service: Any) -> None:
        self.service = service
        self.app = service.app
        self.config = service.config
        self.notes = service.notes

    # -- shared lookups --------------------------------------------------------

    def _role(self, actor: str, allowed: frozenset[str]) -> str:
        role = self.app.state.review_queue.service.authorizer.role(actor)
        if role not in allowed:
            raise ClaimCoreError(
                403, "FORBIDDEN_ROLE", "Role cannot work this approval note"
            )
        return str(role)

    def review(self, review_id: str) -> dict[str, Any]:
        """Return one open-or-resolved `NOTE_REVIEW{approval_note}` row.

        A different type, a different subtype, or another claim's id is
        indistinguishable from absent.
        """

        self.app.state.review_queue.backfill("agent:approval_pack")
        rows = self.service._rows(
            "SELECT id, claim_id, type, subtype, status, payload FROM review_items "
            "WHERE id = :review_id",
            review_id=review_id,
        )
        if not rows:
            raise ClaimCoreError(404, "REVIEW_NOT_FOUND", "Review item was not found")
        row = {**rows[0], "payload": _json(rows[0]["payload"]) or {}}
        if row["type"] != "NOTE_REVIEW" or row["subtype"] != "approval_note":
            raise ClaimCoreError(404, "REVIEW_NOT_FOUND", "Review item was not found")
        return row

    def drafts(self, claim_id: str) -> list[dict[str, Any]]:
        rows = self.service._rows(
            "SELECT id, version, status, body, edited_by, signed_by FROM note_drafts "
            "WHERE claim_id = :claim_id ORDER BY version",
            claim_id=claim_id,
        )
        return [{**row, "body": _json(row["body"]) or {}} for row in rows]

    @staticmethod
    def root_of(draft: dict[str, Any]) -> str:
        lineage = draft["body"].get("lineage")
        if isinstance(lineage, dict) and isinstance(lineage.get("root_draft_id"), str):
            return str(lineage["root_draft_id"])
        return str(draft["id"])

    def lineage(self, claim_id: str, root_draft_id: str) -> list[dict[str, Any]]:
        """Return every retained version of one lineage in version order."""

        return [
            draft
            for draft in self.drafts(claim_id)
            if self.root_of(draft) == root_draft_id
        ]

    def current(self, claim_id: str, root_draft_id: str) -> dict[str, Any]:
        """Return the highest version of the lineage, not the original id."""

        versions = self.lineage(claim_id, root_draft_id)
        if not versions:
            raise ClaimCoreError(404, "REVIEW_NOT_FOUND", "Review item was not found")
        return versions[-1]

    def root_for_review(self, review: dict[str, Any]) -> str:
        recorded = review["payload"].get("root_draft_id")
        if isinstance(recorded, str) and recorded:
            return recorded
        draft_id = review["payload"].get("note_draft_id")
        for draft in self.drafts(str(review["claim_id"])):
            if draft["id"] == draft_id:
                return self.root_of(draft)
        raise ClaimCoreError(
            409, "NOTE_DRAFT_UNAVAILABLE", "The reviewed approval-note draft is absent"
        )

    # -- signability -----------------------------------------------------------

    def blockers(self, claim_id: str, actor: str) -> tuple[list[dict[str, Any]], Any]:
        """Recompute the visible blocker list from current durable inputs.

        C-08 and every uncaptured T-01 slot surface here, so a live motor claim
        is never reported signable (§0).
        """

        readiness = self.service.readiness.evaluate(claim_id, actor)
        try:
            _computed, computed_blockers = self.notes.builder.computed(readiness)
        except NoteInputsInvalid as error:
            return (
                [{"slot": None, "state": "blocked_on_inputs", "detail": str(error)}],
                readiness,
            )
        _rows, verification_blockers, _used = self.notes.builder.verification(claim_id)
        return computed_blockers + verification_blockers, readiness

    def _prepared(self, claim_id: str, draft_id: str) -> dict[str, Any] | None:
        prepared = [
            event
            for event in self.service._events(claim_id, "pack.note_sign_prepared")
            if event["payload"].get("note_draft_id") == draft_id
        ]
        return prepared[-1] if prepared else None

    def _signed(self, claim_id: str, draft_id: str) -> dict[str, Any] | None:
        signed = [
            event
            for event in self.service._events(claim_id, "pack.note_signed")
            if event["payload"].get("note_draft_id") == draft_id
        ]
        return signed[-1] if signed else None

    def sign_state(self, claim_id: str, draft_id: str) -> str:
        """Report the durable signature state; a resolution is never lost (§4)."""

        if self._signed(claim_id, draft_id) is not None:
            return "signed"
        if self._prepared(claim_id, draft_id) is not None:
            return "signing_pending"
        return "unsigned"

    def merged_pack(self, claim_id: str, body: dict[str, Any]) -> dict[str, Any]:
        reference = body.get("merged_pack") or {}
        event_id = reference.get("event_id")
        return {
            "event_id": event_id,
            "version": reference.get("version"),
            "sha256": reference.get("sha256"),
            "content_url": (
                f"/claims/{claim_id}/approval-pack/artifacts/{event_id}"
                if isinstance(event_id, str) and event_id
                else None
            ),
        }

    # -- workspace read --------------------------------------------------------

    def read(self, review_id: str, actor: str) -> dict[str, Any]:
        """Return the complete authenticated NOTE_REVIEW workspace payload."""

        self._role(actor, WORKSPACE_ROLES)
        review = self.review(review_id)
        claim_id = str(review["claim_id"])
        root_draft_id = self.root_for_review(review)
        draft = self.current(claim_id, root_draft_id)
        blockers, _readiness = self.blockers(claim_id, actor)
        signed_state = self.sign_state(claim_id, draft["id"])
        signed_event = self._signed(claim_id, draft["id"])
        body = draft["body"]
        return {
            "review_id": review["id"],
            "review_status": review["status"],
            "claim_id": claim_id,
            "root_draft_id": root_draft_id,
            "current_draft": {
                "id": draft["id"],
                "version": draft["version"],
                "status": draft["status"],
                "body_sha256": canonical_body_hash(body),
                "edited_by": draft["edited_by"],
                "body": body,
            },
            "merged_pack": self.merged_pack(claim_id, body),
            "signed_note": (
                {
                    "event_id": signed_event["id"],
                    "sha256": signed_event["payload"].get("artifact_sha256"),
                    "content_url": (
                        f"/claims/{claim_id}/approval-pack/artifacts/"
                        f"{signed_event['id']}"
                    ),
                }
                if signed_event is not None
                else None
            ),
            "sign_state": signed_state,
            "autosave_seconds": self.config.autosave_seconds,
            "commentary_slots": list(self.config.note["commentary_slots"]),
            "editable_slots": list(self.config.note["commentary_slots"]),
            "incident_summary_max_words": int(
                self.config.commentary["incident_summary_max_words"]
            ),
            "icon_note_entry": self.config.field_set("icon.note_entry"),
            "signable": not blockers,
            "blockers": blockers,
        }

    # -- artifact read ---------------------------------------------------------

    def artifact(self, claim_id: str, event_id: str, actor: str) -> dict[str, Any]:
        """Resolve an allowlisted artifact-index event to its server-owned blob.

        A raw blob key is never accepted from a browser and a cross-claim event
        id is indistinguishable from absent (#253).
        """

        self.service.require_read_role(actor)
        rows = self.service._rows(
            "SELECT id, claim_id, type, payload FROM events WHERE id = :event_id",
            event_id=event_id,
        )
        row = rows[0] if rows else None
        if (
            row is None
            or row["claim_id"] != claim_id
            or row["type"] not in ARTIFACT_EVENT_TYPES
        ):
            raise ClaimCoreError(404, "ARTIFACT_NOT_FOUND", "Artifact was not found")
        payload = _json(row["payload"]) or {}
        blob_key = payload.get(ARTIFACT_EVENT_TYPES[row["type"]])
        digest = payload.get("sha256") or payload.get("artifact_sha256")
        if not isinstance(blob_key, str) or not blob_key:
            raise ClaimCoreError(404, "ARTIFACT_NOT_FOUND", "Artifact was not found")
        try:
            content = self.service.store.get(blob_key)
        except Exception as error:  # noqa: BLE001 - an unreadable blob fails visibly
            raise ClaimCoreError(
                409, "ARTIFACT_UNAVAILABLE", "The immutable artifact is unreadable"
            ) from error
        self.service._emit(
            claim_id=claim_id,
            event_type="pack.artifact_accessed",
            payload={
                "artifact_event_id": event_id,
                "artifact_event_type": row["type"],
                "artifact_sha256": digest if isinstance(digest, str) else None,
                "actor": actor,
            },
            correlation_id=event_id,
            actor=actor,
        )
        return {
            "content": content,
            "sha256": digest if isinstance(digest, str) else None,
            "filename": payload.get("filename") or f"{event_id}.pdf",
        }

    # -- autosave --------------------------------------------------------------

    def _request_digest(
        self,
        *,
        base_draft_id: str,
        base_body_sha256: str,
        commentary: list[dict[str, str]],
    ) -> str:
        return hashlib.sha256(
            canonical_json(
                {
                    "base_body_sha256": base_body_sha256,
                    "base_draft_id": base_draft_id,
                    "commentary": commentary,
                }
            ).encode("utf-8")
        ).hexdigest()

    def _validate_commentary(
        self, claim_id: str, actor: str, readiness: Any, commentary: list[dict[str, str]]
    ) -> dict[str, str]:
        """Accept exactly the configured slots once each, then revalidate the prose.

        The deterministic numeric allow-list and the ≤80-word incident-summary
        limit run on every save: an edited note is held to the same contract as
        a generated one (§3).
        """

        slots = list(self.config.note["commentary_slots"])
        supplied = [str(entry.get("template_slot")) for entry in commentary]
        if supplied != slots:
            raise ClaimCoreError(
                422,
                "COMMENTARY_SLOTS_INVALID",
                f"Commentary must be exactly {slots} in order, once each",
            )
        contents: dict[str, str] = {}
        for entry in commentary:
            content = entry.get("content")
            if not isinstance(content, str):
                raise ClaimCoreError(
                    422, "COMMENTARY_SLOTS_INVALID", "Commentary content must be text"
                )
            contents[str(entry["template_slot"])] = content
        savings = self.notes.builder.savings(claim_id)
        try:
            verified = self.notes.builder.verified_fields(readiness)
        except NoteInputsInvalid as error:
            raise ClaimCoreError(
                409, "SAVE_BLOCKED_ON_INPUTS", "A cited claim field lost its provenance"
            ) from error
        allowed = self.notes.builder.allowed_numbers(verified, savings)
        errors = self.notes.generator.validator.validate(
            {
                "paragraphs": [
                    {
                        "template_slot": slot,
                        "content": contents[slot],
                        # The officer never declares tokens; the server derives
                        # them, so an injected number cannot be smuggled past
                        # the allow-list by omitting it from the declaration.
                        "numbers_used": numeric_tokens(contents[slot]),
                    }
                    for slot in slots
                ]
            },
            allowed,
        )
        if errors:
            raise ClaimCoreError(
                422,
                "COMMENTARY_INVALID",
                "Edited commentary failed its deterministic checks",
                extra={"validation_errors": errors},
            )
        return contents

    def _next_version(self, session: Any, claim_id: str) -> int:
        highest = session.execute(
            text("SELECT MAX(version) FROM note_drafts WHERE claim_id = :claim_id"),
            {"claim_id": claim_id},
        ).scalar()
        return int(highest or 0) + 1

    def _result(self, event: dict[str, Any]) -> dict[str, Any]:
        payload = event["payload"]
        return {
            "draft_id": payload["note_draft_id"],
            "version": payload["note_version"],
            "body_sha256": payload["body_sha256"],
            "parent_draft_id": payload["parent_draft_id"],
            "review_id": payload["review_id"],
            "recorded": False,
        }

    def autosave(
        self,
        review_id: str,
        *,
        actor: str,
        idempotency_key: str,
        base_draft_id: str,
        base_body_sha256: str,
        commentary: list[dict[str, str]],
    ) -> dict[str, Any]:
        """Append exactly one new note version; never update a stored body."""

        self._role(actor, EDIT_ROLES)
        if not idempotency_key.strip():
            raise ClaimCoreError(
                422, "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key must be non-empty"
            )
        digest = self._request_digest(
            base_draft_id=base_draft_id,
            base_body_sha256=base_body_sha256,
            commentary=commentary,
        )
        initial_review = self.review(review_id)
        claim_id = str(initial_review["claim_id"])
        with self.service._claim_guard(claim_id, row_lock=False):
            # Re-read every mutable authority after waiting for the claim guard.
            # A sign resolution may have committed while this save was queued.
            review = self.review(review_id)
            if review["status"] != "open":
                raise ClaimCoreError(
                    409,
                    "ALREADY_RESOLVED",
                    "The approval-note review is no longer open",
                )
            claim, _fields, _blocked = self.app.state.claim_service.hydrate_claim(
                claim_id, actor, paths=[]
            )
            if claim.status != "PACK_READY":
                raise ClaimCoreError(
                    409,
                    "SAVE_BLOCKED_ON_STATE",
                    "An approval note may only be edited while the claim is PACK_READY",
                )
            root_draft_id = self.root_for_review(review)
            _blockers, readiness = self.blockers(claim_id, actor)
            contents = self._validate_commentary(
                claim_id, actor, readiness, commentary
            )
            for event in self.service._events(claim_id, "pack.note_autosaved"):
                if event["payload"].get("idempotency_key") != idempotency_key:
                    continue
                if event["payload"].get("request_sha256") != digest:
                    raise ClaimCoreError(
                        409,
                        "IDEMPOTENCY_CONFLICT",
                        "This idempotency key already recorded a different save",
                    )
                return self._result(event)
            latest = self.current(claim_id, root_draft_id)
            latest_hash = canonical_body_hash(latest["body"])
            if latest["id"] != base_draft_id or latest_hash != base_body_sha256:
                raise ClaimCoreError(
                    409,
                    "STALE_NOTE_DRAFT",
                    "Another tab saved a newer version of this approval note",
                    extra={
                        "current_draft_id": latest["id"],
                        "current_version": latest["version"],
                        "current_body_sha256": latest_hash,
                    },
                )
            if latest["status"] == "signed":
                raise ClaimCoreError(
                    409, "NOTE_ALREADY_SIGNED", "A signed approval note is immutable"
                )
            body = self._edited_body(latest["body"], contents)
            draft_id = new_ulid()
            body["lineage"] = {
                "root_draft_id": root_draft_id,
                "parent_draft_id": latest["id"],
                "review_id": review["id"],
            }
            body["body_sha256"] = canonical_body_hash(body)
            with self.service.sessions.begin() as session:
                version = self._next_version(session, claim_id)
                # Only the immediate unsigned predecessor is superseded: the
                # open NOTE_REVIEW keeps pointing at this one lineage.
                session.execute(
                    text(
                        "UPDATE note_drafts SET status = 'superseded' "
                        "WHERE id = :draft_id AND status IN ('draft', 'in_review')"
                    ),
                    {"draft_id": latest["id"]},
                )
                session.add(
                    NoteDraft(
                        id=draft_id,
                        claim_id=claim_id,
                        version=version,
                        body=body,
                        status="in_review",
                        edited_by=actor,
                        signed_by=None,
                        signed_at=None,
                    )
                )
                self.service._record(
                    session,
                    claim_id=claim_id,
                    event_type="pack.note_autosaved",
                    # Commentary text never enters an event or the ledger (§9).
                    payload={
                        "note_draft_id": draft_id,
                        "note_version": version,
                        "parent_draft_id": latest["id"],
                        "parent_version": latest["version"],
                        "root_draft_id": root_draft_id,
                        "review_id": review["id"],
                        "body_sha256": body["body_sha256"],
                        "parent_body_sha256": latest_hash,
                        "idempotency_key": idempotency_key,
                        "request_sha256": digest,
                        "actor": actor,
                    },
                    correlation_id=review["id"],
                    actor=actor,
                )
        return {
            "draft_id": draft_id,
            "version": version,
            "body_sha256": body["body_sha256"],
            "parent_draft_id": latest["id"],
            "review_id": review["id"],
            "recorded": True,
        }

    def _edited_body(
        self, base: dict[str, Any], contents: dict[str, str]
    ) -> dict[str, Any]:
        """Copy every server-owned section forward and replace only the prose.

        Computed and verification sections, citations, blockers, pack refs,
        template id/version and integrity refs are taken from the stored body,
        so a client cannot supply or change one (§3).
        """

        body = _copy(base)
        slots = set(self.config.note["commentary_slots"])
        sections = []
        for section in body.get("sections", []):
            slot = section.get("template_slot")
            if slot in slots:
                section = {
                    **section,
                    "content": contents[str(slot)],
                    "locked": False,
                    "numbers_used": numeric_tokens(contents[str(slot)]),
                }
            sections.append(section)
        body["sections"] = sections
        body["signable"] = False
        return body


def _copy(value: Any) -> Any:
    """Return a structural copy without importing a deep-copy of live objects."""

    if isinstance(value, dict):
        return {key: _copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy(item) for item in value]
    return value


__all__ = ["ARTIFACT_EVENT_TYPES", "EDIT_ROLES", "NoteWorkspace", "WORKSPACE_ROLES"]
