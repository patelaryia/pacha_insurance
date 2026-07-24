"""The Temporal environment contract (master plan section 6).

This module is the only place that reads Temporal environment variables, and it
reads exactly the twelve the master plan declares — no more, no defaults
invented, no API-key fallback. Every rule is checked at construction so that a
misconfigured deployment raises before a client connects or a Worker polls,
rather than surfacing as a half-started Worker holding a Task Queue open.

The strict ones worth stating plainly:

* `staging` and `prod` refuse `local` mode outright;
* cloud mode requires the full mTLS and KMS set — there is no partial cloud;
* the queue prefix must be exactly `pacha-{env}`, so a staging Worker cannot
  poll a production queue by typo;
* the build ID must be a full git commit SHA, because Worker Deployment
  versioning pins finite executions to it;
* the KMS variable must be an immutable key ARN, never an alias ARN — see the
  rotation note beside `_KMS_ARN_PATTERN`.

Register #285: section 6 also requires `PACHA_TEMPORAL_REGION` to "match the
approved namespace", but no approved region-to-namespace mapping exists in any
source document. The narrowest safe behaviour is implemented here — the region
is mandatory in cloud mode and must be a well-formed AWS region token — and the
missing allowlist is registered rather than guessed.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from orchestration.errors import ConfigurationError

__all__ = [
    "ENVIRONMENTS",
    "TEMPORAL_MODES",
    "WORKER_ROLES",
    "TemporalConfig",
]

ENVIRONMENTS: Final[tuple[str, ...]] = ("dev", "test", "staging", "prod")
TEMPORAL_MODES: Final[tuple[str, ...]] = ("local", "cloud")
WORKER_ROLES: Final[tuple[str, ...]] = ("control", "docintel", "effects", "ledger")

#: Environments in which `local` mode and static Codec keys are permitted.
NON_PRODUCTION_ENVIRONMENTS: Final[frozenset[str]] = frozenset({"dev", "test"})

_DEFAULT_LOCAL_ADDRESS: Final[str] = "localhost:7233"

_ADDRESS_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?:\d{1,5}$")
_NAMESPACE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REGION_PATTERN = re.compile(r"^[a-z]{2}(?:-[a-z]+)+-\d$")
_BUILD_ID_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_SECRET_ARN_PATTERN = re.compile(
    r"^arn:aws[a-z-]*:secretsmanager:[a-z0-9-]+:\d{12}:secret:[A-Za-z0-9/_+=.@-]+$"
)
# Key ARNs only. An alias ARN is refused: `GenerateDataKey` returns the
# canonical key ARN in `KeyId` however the key was addressed, so an alias-pinned
# Codec allowlist rejects its own data key and encoding fails at runtime. Key
# material is rotated in KMS, which preserves the ARN (master plan §11).
_KMS_ARN_PATTERN = re.compile(
    r"^arn:aws[a-z-]*:kms:[a-z0-9-]+:\d{12}:key/[0-9a-f]{8}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_KMS_ALIAS_ARN_PATTERN = re.compile(r"^arn:aws[a-z-]*:kms:[a-z0-9-]+:\d{12}:alias/")


def _require(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"{name} is required")
    return value


def _require_cloud(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"{name} is required in cloud mode")
    return value


def _match(name: str, value: str, pattern: re.Pattern[str], expectation: str) -> str:
    if not pattern.fullmatch(value):
        raise ConfigurationError(f"{name} must be {expectation}")
    return value


@dataclass(frozen=True, slots=True)
class TemporalConfig:
    """A validated Temporal connection and Worker identity.

    Constructing one is the validation: every instance in existence satisfies
    section 6 in full.
    """

    env: str
    mode: str
    address: str
    namespace: str
    queue_prefix: str
    build_id: str
    region: str | None = None
    tls_cert_secret_arn: str | None = None
    tls_key_secret_arn: str | None = None
    kms_key_arn: str | None = None
    worker_role: str | None = None

    def __post_init__(self) -> None:
        if self.env not in ENVIRONMENTS:
            raise ConfigurationError("PACHA_ENV must be dev, test, staging or prod")
        if self.mode not in TEMPORAL_MODES:
            raise ConfigurationError("PACHA_TEMPORAL_MODE must be local or cloud")
        if self.mode == "local" and self.env not in NON_PRODUCTION_ENVIRONMENTS:
            raise ConfigurationError(
                f"PACHA_TEMPORAL_MODE=local is refused in {self.env}; cloud mode is mandatory"
            )

        _match("PACHA_TEMPORAL_ADDRESS", self.address, _ADDRESS_PATTERN, "host:port")
        _match(
            "PACHA_TEMPORAL_NAMESPACE",
            self.namespace,
            _NAMESPACE_PATTERN,
            "an explicit namespace name",
        )
        if self.namespace == "default" and self.env not in NON_PRODUCTION_ENVIRONMENTS:
            raise ConfigurationError(
                "PACHA_TEMPORAL_NAMESPACE must be explicit; 'default' is refused outside dev/test"
            )
        if self.queue_prefix != f"pacha-{self.env}":
            raise ConfigurationError(
                f"PACHA_TEMPORAL_QUEUE_PREFIX must be exactly pacha-{self.env}"
            )
        _match(
            "PACHA_BUILD_ID",
            self.build_id,
            _BUILD_ID_PATTERN,
            "the 40-character git commit SHA of the deployed image",
        )
        if self.worker_role is not None and self.worker_role not in WORKER_ROLES:
            raise ConfigurationError(
                "PACHA_WORKER_ROLE must be control, docintel, effects or ledger"
            )

        if self.mode == "cloud":
            self._validate_cloud()

    def _validate_cloud(self) -> None:
        if self.region is None:
            raise ConfigurationError("PACHA_TEMPORAL_REGION is required in cloud mode")
        _match("PACHA_TEMPORAL_REGION", self.region, _REGION_PATTERN, "an AWS region token")
        if self.tls_cert_secret_arn is None or self.tls_key_secret_arn is None:
            raise ConfigurationError(
                "cloud mode requires both mTLS Secrets Manager ARNs; there is no API-key fallback"
            )
        _match(
            "PACHA_TEMPORAL_TLS_CERT_SECRET_ARN",
            self.tls_cert_secret_arn,
            _SECRET_ARN_PATTERN,
            "a Secrets Manager secret ARN",
        )
        _match(
            "PACHA_TEMPORAL_TLS_KEY_SECRET_ARN",
            self.tls_key_secret_arn,
            _SECRET_ARN_PATTERN,
            "a Secrets Manager secret ARN",
        )
        if self.kms_key_arn is None:
            raise ConfigurationError("PACHA_TEMPORAL_KMS_KEY_ARN is required in cloud mode")
        if _KMS_ALIAS_ARN_PATTERN.match(self.kms_key_arn):
            raise ConfigurationError(
                "PACHA_TEMPORAL_KMS_KEY_ARN must be an immutable key ARN, not an alias ARN; "
                "KMS returns the canonical key ARN and an alias-pinned Codec allowlist "
                "would reject its own data key"
            )
        _match(
            "PACHA_TEMPORAL_KMS_KEY_ARN",
            self.kms_key_arn,
            _KMS_ARN_PATTERN,
            "an immutable KMS key ARN (arn:aws:kms:{region}:{account}:key/{uuid})",
        )

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        require_worker_role: bool = False,
    ) -> TemporalConfig:
        """Read and validate the section 6 environment contract.

        Args:
            environ: the mapping to read; defaults to the process environment.
            require_worker_role: set by Worker entry points, for which
                `PACHA_WORKER_ROLE` is mandatory.

        Raises:
            ConfigurationError: any variable is missing, malformed or forbidden
                for the declared environment.
        """

        source = os.environ if environ is None else environ

        env = _require(source, "PACHA_ENV")
        mode = _require(source, "PACHA_TEMPORAL_MODE")
        if env not in ENVIRONMENTS:
            raise ConfigurationError("PACHA_ENV must be dev, test, staging or prod")
        if mode not in TEMPORAL_MODES:
            raise ConfigurationError("PACHA_TEMPORAL_MODE must be local or cloud")

        if mode == "cloud":
            address = _require_cloud(source, "PACHA_TEMPORAL_ADDRESS")
            region: str | None = _require_cloud(source, "PACHA_TEMPORAL_REGION")
            cert_arn: str | None = _require_cloud(source, "PACHA_TEMPORAL_TLS_CERT_SECRET_ARN")
            key_arn: str | None = _require_cloud(source, "PACHA_TEMPORAL_TLS_KEY_SECRET_ARN")
            kms_arn: str | None = _require_cloud(source, "PACHA_TEMPORAL_KMS_KEY_ARN")
        else:
            address = source.get("PACHA_TEMPORAL_ADDRESS", "").strip() or _DEFAULT_LOCAL_ADDRESS
            region = None
            cert_arn = None
            key_arn = None
            kms_arn = None

        worker_role = source.get("PACHA_WORKER_ROLE", "").strip() or None
        if require_worker_role and worker_role is None:
            raise ConfigurationError("PACHA_WORKER_ROLE is required to start a Worker")

        return cls(
            env=env,
            mode=mode,
            address=address,
            namespace=_require(source, "PACHA_TEMPORAL_NAMESPACE"),
            queue_prefix=_require(source, "PACHA_TEMPORAL_QUEUE_PREFIX"),
            build_id=_require(source, "PACHA_BUILD_ID"),
            region=region,
            tls_cert_secret_arn=cert_arn,
            tls_key_secret_arn=key_arn,
            kms_key_arn=kms_arn,
            worker_role=worker_role,
        )

    @property
    def is_cloud(self) -> bool:
        """True when this process must connect over mTLS to Temporal Cloud."""

        return self.mode == "cloud"

    @property
    def is_production_like(self) -> bool:
        """True in `staging` and `prod`, where test seams are refused."""

        return self.env not in NON_PRODUCTION_ENVIRONMENTS

    def task_queue(self, role: str | None = None) -> str:
        """`{prefix}-{role}-v1` for one Worker role (section 7)."""

        resolved = role or self.worker_role
        if resolved is None:
            raise ConfigurationError("a Worker role is required to build a Task Queue name")
        if resolved not in WORKER_ROLES:
            raise ConfigurationError(
                "PACHA_WORKER_ROLE must be control, docintel, effects or ledger"
            )
        return f"{self.queue_prefix}-{resolved}-v1"

    def deployment_name(self, role: str | None = None) -> str:
        """`pacha-{env}-{role}` — the Worker Deployment name (section 8)."""

        resolved = role or self.worker_role
        if resolved is None:
            raise ConfigurationError("a Worker role is required to build a Deployment name")
        if resolved not in WORKER_ROLES:
            raise ConfigurationError(
                "PACHA_WORKER_ROLE must be control, docintel, effects or ledger"
            )
        return f"pacha-{self.env}-{resolved}"
