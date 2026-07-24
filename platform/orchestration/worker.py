"""Worker construction (master plan sections 7 and 8).

Registrations are explicit. There is no discovery, no reflection and no plugin
registry: a later packet hands this factory the exact Workflow and Activity
objects its role owns, which is what keeps the deployed registration set
reviewable and keeps a stray import from silently widening a Worker's surface.

Role determines everything else — Task Queue, Activity concurrency and
Deployment name — so a `ledger` Worker is structurally incapable of polling the
control queue, and its concurrency of one reinforces the single-writer ledger
rule that the PostgreSQL advisory lock enforces.

Every Worker is pinned. `VersioningBehavior.PINNED` keeps a finite execution on
the build that started it, and `PACHA_BUILD_ID` is the immutable git SHA of the
deployed image, so an in-flight Workflow never replays against code it did not
start on.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any, Final

from temporalio.client import Client
from temporalio.common import VersioningBehavior
from temporalio.worker import Worker, WorkerDeploymentConfig, WorkerDeploymentVersion
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from orchestration.config import WORKER_ROLES, TemporalConfig
from orchestration.contracts import load_control_registries
from orchestration.errors import ConfigurationError
from orchestration.policies import load_retry_policies

__all__ = [
    "ROLE_ACTIVITY_CONCURRENCY",
    "WORKER_GRACEFUL_SHUTDOWN",
    "WORKFLOW_SAFE_MODULES",
    "build_worker",
    "default_workflow_runner",
]

#: Section 7 — Worker Activity concurrency per role.
ROLE_ACTIVITY_CONCURRENCY: Final[dict[str, int]] = {
    "control": 20,
    "docintel": 4,
    "effects": 5,
    "ledger": 1,
}

#: Section 7 — Worker graceful shutdown; ECS stop timeout is 120 seconds.
WORKER_GRACEFUL_SHUTDOWN: Final[timedelta] = timedelta(seconds=60)


#: The only modules Workflow code may share with the host process.
#:
#: Each is deterministic and holds no mutable state beyond a cache of immutable
#: pack data: contracts and IDs are pure validation, errors is a closed
#: taxonomy, policies is parsed pack YAML. They are passed through because
#: Workflow code legitimately constructs contracts and reads retry policies,
#: and re-importing them inside the sandbox would turn every contract
#: validation into a restricted pack-file read.
#:
#: Deliberately absent: `orchestration.client`, `orchestration.codec`,
#: `orchestration.config` and `orchestration.worker`. Those carry configuration,
#: credentials, KMS calls and `os.urandom`, none of which belongs in a
#: deterministic replay context. The package root is not passed through either,
#: which is why `orchestration.__init__` resolves its exports lazily — an eager
#: `__init__` would drag the client and Codec back in behind these four.
WORKFLOW_SAFE_MODULES: Final[tuple[str, ...]] = (
    "orchestration.contracts",
    "orchestration.errors",
    "orchestration.ids",
    "orchestration.policies",
)


def default_workflow_runner() -> SandboxedWorkflowRunner:
    """The sandboxed runner every Pacha Worker uses."""

    # Warm the pack caches in the host process so the sandbox never reads a file.
    load_control_registries()
    load_retry_policies()
    return SandboxedWorkflowRunner(
        restrictions=SandboxRestrictions.default.with_passthrough_modules(*WORKFLOW_SAFE_MODULES)
    )


def build_worker(
    client: Client,
    config: TemporalConfig,
    *,
    role: str | None = None,
    workflows: Sequence[type] = (),
    activities: Sequence[Callable[..., Any]] = (),
    **worker_options: Any,
) -> Worker:
    """Build the Worker for one role with its exact registrations.

    Args:
        client: a client built by `build_temporal_client`, carrying the
            encrypted Data Converter.
        config: a validated section 6 configuration.
        role: the Worker role; defaults to `PACHA_WORKER_ROLE`.
        workflows: the Workflow classes this Worker registers, explicitly.
        activities: the Activity functions this Worker registers, explicitly.
        **worker_options: passed through to `Worker` for options this factory
            does not fix.

    Raises:
        ConfigurationError: the role is unknown, no role is available, nothing
            was registered, or a caller tried to override a fixed option.
    """

    resolved = role or config.worker_role
    if resolved is None:
        raise ConfigurationError("PACHA_WORKER_ROLE is required to start a Worker")
    if resolved not in WORKER_ROLES:
        raise ConfigurationError("PACHA_WORKER_ROLE must be control, docintel, effects or ledger")
    if not workflows and not activities:
        raise ConfigurationError(
            "a Worker needs explicit Workflow or Activity registrations; "
            "this factory does not discover them"
        )

    fixed = {
        "task_queue",
        "workflows",
        "activities",
        "deployment_config",
        "use_worker_versioning",
        "graceful_shutdown_timeout",
        "max_concurrent_activities",
    }
    overridden = fixed & set(worker_options)
    if overridden:
        raise ConfigurationError(
            f"these Worker options are fixed by role and may not be overridden: "
            f"{', '.join(sorted(overridden))}"
        )

    deployment_config = WorkerDeploymentConfig(
        version=WorkerDeploymentVersion(
            deployment_name=config.deployment_name(resolved),
            build_id=config.build_id,
        ),
        use_worker_versioning=True,
        default_versioning_behavior=VersioningBehavior.PINNED,
    )

    worker_options.setdefault("workflow_runner", default_workflow_runner())

    return Worker(
        client,
        task_queue=config.task_queue(resolved),
        workflows=list(workflows),
        activities=list(activities),
        deployment_config=deployment_config,
        graceful_shutdown_timeout=WORKER_GRACEFUL_SHUTDOWN,
        max_concurrent_activities=ROLE_ACTIVITY_CONCURRENCY[resolved],
        **worker_options,
    )
