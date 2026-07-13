"""Correction capture, labelled corpus storage, and synchronous batch evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Any, Protocol

from sqlalchemy import select, text

from claim_core import new_ulid
from eval_harness.models import TestCase

CAPABILITY_TAG = "capability:"
TYPED_KINDS = frozenset({"money", "date", "party", "enum", "text"})


@dataclass(frozen=True)
class CorpusObservation:
    """One grader subject returned by an isolated corpus replay adapter."""

    capability_id: str
    grader_id: str
    subject_ref: dict[str, Any]


class CorpusExecutor(Protocol):
    """Boundary implemented by synthetic fixtures and the future replay adapter."""

    def execute(self, case: TestCase) -> list[CorpusObservation]: ...


@dataclass(frozen=True)
class CorpusBatchResult:
    """Exact full-corpus execution totals and per-capability scorecards."""

    corpus: str
    total_cases: int
    runnable_cases: int
    blocked_cases: int
    total_grades: int
    elapsed_ms: int
    scorecards: dict[str, dict[str, int | float]]


def _case_capabilities(case: TestCase) -> list[str]:
    return [
        tag.removeprefix(CAPABILITY_TAG)
        for tag in case.tags or []
        if isinstance(tag, str)
        and tag.startswith(CAPABILITY_TAG)
        and len(tag) > len(CAPABILITY_TAG)
    ]


def _empty_scorecard() -> dict[str, int | float]:
    return {
        "cases": 0,
        "grades": 0,
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "blocked": 0,
        "pass_percent": 0,
    }


class CorpusService:
    """Persist correction evidence and drive batch grading through one executor seam."""

    def __init__(
        self,
        harness: Any,
        executor: CorpusExecutor | None,
        weekly_config: dict[str, Any],
    ) -> None:
        self.harness = harness
        self.executor = executor
        self.weekly_config = self._validate_weekly(weekly_config)

    @staticmethod
    def _validate_weekly(config: dict[str, Any]) -> dict[str, Any]:
        enabled = config.get("enabled")
        interval = config.get("interval_seconds")
        corpus = config.get("corpus")
        if (
            not isinstance(enabled, bool)
            or not isinstance(interval, int)
            or isinstance(interval, bool)
            or interval != 7 * 24 * 60 * 60
            or not isinstance(corpus, str)
            or not corpus
        ):
            raise ValueError("weekly eval config requires enabled, seven days, and corpus")
        return dict(config)

    def create_case(
        self,
        *,
        corpus: str,
        origin: str,
        input_bundle: dict[str, Any],
        expected: dict[str, Any],
        tags: list[str],
    ) -> str:
        """Create one immutable case using the already-migrated PRD-03 table."""

        if not isinstance(corpus, str) or not corpus:
            raise ValueError("corpus must be a non-empty string")
        if origin not in {"seed_closed_claim", "production_correction"}:
            raise ValueError("invalid corpus case origin")
        if not isinstance(input_bundle, dict) or not isinstance(expected, dict):
            raise ValueError("input_bundle and expected must be mappings")
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise ValueError("tags must be strings")
        case_id = new_ulid()
        with self.harness.sessions.begin() as session:
            session.add(
                TestCase(
                    id=case_id,
                    corpus=corpus,
                    origin=origin,
                    input_bundle=dict(input_bundle),
                    expected=dict(expected),
                    tags=list(dict.fromkeys(tags)),
                    created_at=self.harness.clock(),
                )
            )
        return case_id

    def _captured(self, source_event_id: str) -> bool:
        with self.harness.sessions() as session:
            cases = session.scalars(
                select(TestCase).where(TestCase.origin == "production_correction")
            )
            return any(
                case.input_bundle.get("source_event_id") == source_event_id for case in cases
            )

    def _claim_context(self, claim_id: str) -> tuple[str | None, list[dict[str, str]]]:
        with self.harness.engine.connect() as connection:
            pack_version = connection.execute(
                text("SELECT pack_version FROM claims WHERE id = :claim_id"),
                {"claim_id": claim_id},
            ).scalar()
            rows = connection.execute(
                text(
                    "SELECT id, s3_key, sha256 FROM documents WHERE claim_id = :claim_id "
                    "ORDER BY received_at, id"
                ),
                {"claim_id": claim_id},
            ).mappings()
            documents = [
                {
                    "document_id": str(row["id"]),
                    "blob_ref": str(row["s3_key"]),
                    "sha256": str(row["sha256"]),
                }
                for row in rows
            ]
        return (pack_version if isinstance(pack_version, str) else None), documents

    def _capture(self, event: Any) -> None:
        if self._captured(event.id):
            return
        payload = event.payload if isinstance(event.payload, dict) else {}
        resolution = payload.get("resolution")
        if resolution in {"approved", "approve_unchanged"}:
            return
        claim_id = event.claim_id
        capability = payload.get("capability_id")
        diff = payload.get("diff")
        changes = diff.get("typed_changes") if isinstance(diff, dict) else None
        changes = changes if isinstance(changes, list) else []
        prose_ratio = diff.get("prose_change_ratio") if isinstance(diff, dict) else None
        prose_ref = (
            diff.get("corrected_prose_ref") if isinstance(diff, dict) else None
        )
        if not isinstance(prose_ref, str):
            prose_ref = payload.get("corrected_prose_ref")
        missing: list[str] = []
        if not isinstance(claim_id, str):
            missing.append("claim_id")
        if not isinstance(capability, str) or not capability:
            missing.append("capability_id")
        if resolution not in {"edited", "rejected"}:
            missing.append("known_resolution")
        if (
            isinstance(prose_ratio, (int, float))
            and not isinstance(prose_ratio, bool)
            and prose_ratio > 0
            and (not isinstance(prose_ref, str) or not prose_ref)
        ):
            missing.append("corrected_prose_ref")

        paths: list[str] = []
        kinds: list[str] = []
        for change in changes:
            if not isinstance(change, dict):
                missing.append("typed_change")
                continue
            path = change.get("path")
            kind = change.get("kind")
            if not isinstance(path, str) or not path:
                missing.append("typed_change.path")
                continue
            if kind not in TYPED_KINDS:
                missing.append(f"typed_change.kind:{path}")
                continue
            if path not in paths:
                paths.append(path)
            if kind not in kinds:
                kinds.append(kind)
        if resolution in {"edited", "rejected"} and not paths:
            missing.append("corrected_path")

        pack_version: str | None = None
        documents: list[dict[str, str]] = []
        fields: dict[str, Any] = {}
        if isinstance(claim_id, str):
            pack_version, documents = self._claim_context(claim_id)
            if pack_version is None:
                missing.append("pinned_pack_version")
            if paths:
                try:
                    _claim, current_fields, _blocked = self.harness.claim_service.hydrate_claim(
                        claim_id,
                        event.actor,
                        paths=paths,
                    )
                except Exception:  # noqa: BLE001 - evidence remains visibly blocked
                    current_fields = {}
                for path in paths:
                    field = current_fields.get(path)
                    if field is None:
                        missing.append(f"current_field:{path}")
                    else:
                        fields[path] = field.value

        input_bundle: dict[str, Any] = {
            "source_event_id": event.id,
            "claim_id": claim_id,
            "pack_version": pack_version,
            "documents": documents,
        }
        if isinstance(prose_ref, str) and prose_ref:
            input_bundle["corrected_prose_ref"] = prose_ref
        expected: dict[str, Any] = {"fields": fields}
        if missing:
            expected["_capture"] = {
                "status": "blocked_on_inputs",
                "missing_inputs": list(dict.fromkeys(missing)),
            }
        tags: list[str] = []
        if isinstance(capability, str) and capability:
            tags.append(f"capability:{capability}")
        tags.append(f"failure_mode:{resolution}")
        tags.extend(f"kind:{kind}" for kind in kinds)
        self.create_case(
            corpus=self.weekly_config["corpus"],
            origin="production_correction",
            input_bundle=input_bundle,
            expected=expected,
            tags=tags,
        )

    def consume(self, event: Any) -> None:
        """Idempotently consume only review resolutions."""

        if event.type == "review.resolved":
            self._capture(event)

    @staticmethod
    def _blocked(case: TestCase) -> bool:
        capture = case.expected.get("_capture")
        return isinstance(capture, dict) and capture.get("status") == "blocked_on_inputs"

    @staticmethod
    def _finalise_scorecards(
        scorecards: dict[str, dict[str, int | float]],
    ) -> dict[str, dict[str, int | float]]:
        for card in scorecards.values():
            denominator = (
                int(card["passed"])
                + int(card["failed"])
                + int(card["errors"])
                + int(card["blocked"])
            )
            card["pass_percent"] = int(card["passed"]) * 100 / denominator if denominator else 0
        return dict(sorted(scorecards.items()))

    def run(
        self,
        *,
        corpus: str,
        capability_id: str | None,
        actor: str,
    ) -> CorpusBatchResult:
        """Run every selected case synchronously and aggregate exact outcomes."""

        started = monotonic()
        with self.harness.sessions() as session:
            rows = list(
                session.scalars(
                    select(TestCase)
                    .where(TestCase.corpus == corpus)
                    .order_by(TestCase.created_at, TestCase.id)
                )
            )
            for row in rows:
                session.expunge(row)
        if capability_id is not None:
            rows = [row for row in rows if capability_id in _case_capabilities(row)]

        scorecards: dict[str, dict[str, int | float]] = {}
        runnable = 0
        blocked = 0
        grades = 0
        for case in rows:
            case_capabilities = _case_capabilities(case)
            for capability in case_capabilities:
                scorecards.setdefault(capability, _empty_scorecard())["cases"] += 1
            if self._blocked(case) or self.executor is None:
                blocked += 1
                for capability in case_capabilities:
                    scorecards.setdefault(capability, _empty_scorecard())["blocked"] += 1
                continue
            runnable += 1
            try:
                observations = self.executor.execute(case)
                if not isinstance(observations, list):
                    raise TypeError("corpus executor must return a list")
            except Exception:  # noqa: BLE001 - executor failures are scorecard errors
                for capability in case_capabilities:
                    scorecards.setdefault(capability, _empty_scorecard())["errors"] += 1
                continue
            if not observations:
                for capability in case_capabilities:
                    scorecards.setdefault(capability, _empty_scorecard())["errors"] += 1
                continue
            for observation in observations:
                if not isinstance(observation, CorpusObservation):
                    for capability in case_capabilities:
                        scorecards.setdefault(capability, _empty_scorecard())["errors"] += 1
                    continue
                if observation.capability_id not in case_capabilities:
                    for capability in case_capabilities:
                        scorecards.setdefault(capability, _empty_scorecard())["errors"] += 1
                    continue
                subject_ref = {
                    **observation.subject_ref,
                    "test_case_id": case.id,
                    "capability_id": observation.capability_id,
                }
                result = self.harness.grade(
                    observation.grader_id,
                    subject_ref,
                    actor,
                )
                card = scorecards.setdefault(observation.capability_id, _empty_scorecard())
                card["grades"] += 1
                card[{"pass": "passed", "fail": "failed", "error": "errors"}[result.result]] += 1
                grades += 1

        return CorpusBatchResult(
            corpus=corpus,
            total_cases=len(rows),
            runnable_cases=runnable,
            blocked_cases=blocked,
            total_grades=grades,
            elapsed_ms=max(0, int((monotonic() - started) * 1000)),
            scorecards=self._finalise_scorecards(scorecards),
        )

    def run_weekly(self, *, actor: str) -> CorpusBatchResult:
        """Direct operations/test seam used by the named Celery task."""

        if not self.weekly_config["enabled"]:
            raise RuntimeError("weekly corpus execution is disabled by pack config")
        return self.run(
            corpus=self.weekly_config["corpus"],
            capability_id=None,
            actor=actor,
        )


__all__ = [
    "CorpusBatchResult",
    "CorpusExecutor",
    "CorpusObservation",
    "CorpusService",
]
