"""Built-in outbox consumers registered by the application factory."""

from __future__ import annotations

from sqlalchemy.orm import attributes, sessionmaker

from claim_core.models import Claim, ClaimField, Event


class ExternalRefsConsumer:
    """The sole writer of the denormalised ``claims.external_refs`` cache."""

    def __init__(self, session_factory: sessionmaker) -> None:
        self._sessions = session_factory

    def __call__(self, event: Event) -> None:
        if event.type != "field.updated":
            return
        path = event.payload.get("path")
        field_id = event.payload.get("field_id")
        if not isinstance(path, str) or not path.startswith("external."):
            return
        with self._sessions.begin() as session:
            field = session.get(ClaimField, field_id)
            if field is None or field.path != path:
                return
            claim = session.get(Claim, field.claim_id)
            if claim is None:
                return
            refs = dict(claim.external_refs or {})
            refs[path] = field.value
            claim.external_refs = refs
            attributes.flag_modified(claim, "external_refs")
