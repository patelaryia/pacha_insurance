"""Alembic environment for the claim-core package."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from agent_runtime.models import AgentRun  # noqa: F401 - registers PACKET-13 metadata
from approval_pack_agent.models import NoteDraft  # noqa: F401 - PACKET-18 metadata
from assessment_agent.models import SavingsLedger  # noqa: F401 - PACKET-17 metadata
from assessment_agent.vendors import Vendor  # noqa: F401 - PACKET-16 metadata
from chase_agent.models import ChaseChecklist, ChaseItem  # noqa: F401 - PACKET-15 metadata
from claim_core.models import Base
from doc_intel.stages import DocumentStage  # noqa: F401 - registers Packet-04 metadata
from projection_agent.models import Projection  # noqa: F401 - PACKET-20 metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
