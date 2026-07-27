"""Make `agent_runs` the binding Temporal operational projection.

Revision ID: 0016_temporal_runtime
Revises: 0015_projections

Register #284: Temporal needs a stable Workflow identity and projection
metadata that the original AR-1 DDL did not carry. The owner-approved schema is
now binding in Section 0.5 and master plan §13.

There is no live data, so the backfill exists for development databases only.
It is still exact rather than best-effort: an existing row is legacy Celery
runner work, so it gets a legacy marker that is deliberately *not* a legal
Temporal Workflow ID (`pacha.legacy.agent.` is outside the §9 kind list). A
value that cannot be handed to the starter cannot be mistaken for one.

The downgrade refuses rather than corrupts. `pending` and `cancelled` have no
representation in the old five-value check, so a database holding either is left
untouched with a clear error instead of having those rows silently relabelled.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_temporal_runtime"
down_revision: str | None = "0015_projections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_COLUMNS: tuple[str, ...] = (
    "workflow_id",
    "workflow_run_id",
    "workflow_type",
    "worker_build_id",
    "last_workflow_event_ref",
    "last_synced_at",
)

_OLD_STATUS_CHECK = "status IN ('running', 'awaiting_review', 'completed', 'failed', 'blocked')"
_NEW_STATUS_CHECK = (
    "status IN ('pending', 'running', 'awaiting_review', 'blocked', "
    "'completed', 'failed', 'cancelled')"
)

#: Statuses the pre-Temporal check cannot express. Never mapped, never deleted.
_UNREPRESENTABLE_STATUSES: tuple[str, ...] = ("pending", "cancelled")


def _json_type() -> sa.types.TypeEngine:
    return postgresql.JSONB() if op.get_bind().dialect.name == "postgresql" else sa.JSON()


def _table(
    *,
    status_check: str,
    identity_nullable: bool,
    unique_workflow_id: bool,
) -> sa.Table:
    """The current `agent_runs` definition SQLite batch mode copies from.

    SQLite cannot alter nullability, drop a check or add a table-level unique
    constraint in place, so Alembic recreates the table and needs the definition
    it is recreating *from*. It must be exact in both directions: omitting the
    foreign key would silently drop it, and omitting a constraint the batch then
    tries to drop raises rather than being ignored.
    """

    json_value = _json_type()
    columns: list[sa.Column] = [
        sa.Column("id", sa.Text(), primary_key=True, comment="ULID correlation id"),
        sa.Column("agent", sa.Text(), nullable=False),
        sa.Column("capability_id", sa.Text(), nullable=False),
        sa.Column("claim_id", sa.Text(), nullable=True),
        sa.Column("trigger_event", sa.Text(), sa.ForeignKey("events.id"), nullable=True),
        sa.Column("workflow_id", sa.Text(), nullable=identity_nullable),
        sa.Column("workflow_run_id", sa.Text(), nullable=True),
        sa.Column("workflow_type", sa.Text(), nullable=identity_nullable),
        sa.Column("worker_build_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("steps", json_value, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("autonomy_level", sa.Text(), nullable=False),
        sa.Column("error", json_value, nullable=True),
        sa.Column("last_workflow_event_ref", sa.Text(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(status_check, name="ck_agent_runs_status"),
    ]
    if unique_workflow_id:
        columns.append(sa.UniqueConstraint("workflow_id", name="uq_agent_runs_workflow_id"))
    return sa.Table(
        "agent_runs",
        sa.MetaData(),
        *columns,
        comment="Durable AR-1 agent execution record.",
    )


def upgrade() -> None:
    """Widen `agent_runs` into the master plan §13 projection, in order."""

    # 1. The six new columns, nullable, so existing rows survive the addition.
    op.add_column("agent_runs", sa.Column("workflow_id", sa.Text(), nullable=True))
    op.add_column("agent_runs", sa.Column("workflow_run_id", sa.Text(), nullable=True))
    op.add_column("agent_runs", sa.Column("workflow_type", sa.Text(), nullable=True))
    op.add_column("agent_runs", sa.Column("worker_build_id", sa.Text(), nullable=True))
    op.add_column("agent_runs", sa.Column("last_workflow_event_ref", sa.Text(), nullable=True))
    op.add_column(
        "agent_runs",
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
    )

    # 2. Exact legacy backfill. `workflow_run_id`, `last_workflow_event_ref` and
    #    `last_synced_at` stay null: no Temporal execution ever observed them.
    op.execute(
        sa.text(
            "UPDATE agent_runs SET "
            "workflow_id = 'pacha.legacy.agent.' || id, "
            "workflow_type = 'LegacyAgentRun', "
            "worker_build_id = 'legacy-celery'"
        )
    )

    with op.batch_alter_table(
        "agent_runs",
        copy_from=_table(
            status_check=_OLD_STATUS_CHECK,
            identity_nullable=True,
            unique_workflow_id=False,
        ),
    ) as batch_op:
        # 3. Identity is mandatory once every row has one.
        batch_op.alter_column("workflow_id", existing_type=sa.Text(), nullable=False)
        batch_op.alter_column("workflow_type", existing_type=sa.Text(), nullable=False)
        # 4. The seven-value status set replaces the five-value one.
        batch_op.drop_constraint("ck_agent_runs_status", type_="check")
        batch_op.create_check_constraint("ck_agent_runs_status", _NEW_STATUS_CHECK)
        # 5. One stable Workflow identity per run.
        batch_op.create_unique_constraint("uq_agent_runs_workflow_id", ["workflow_id"])

    # 6. The two console/operations read paths.
    op.create_index("ix_agent_runs_status", "agent_runs", ["status"])
    op.create_index("ix_agent_runs_claim", "agent_runs", ["claim_id"])


def downgrade() -> None:
    """Reverse the projection widening, refusing to corrupt Temporal-era rows."""

    # Checked before any DDL so a refusal leaves the migration wholly unapplied.
    bind = op.get_bind()
    blocked = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM agent_runs WHERE status IN ('pending', 'cancelled')"
        )
    ).scalar_one()
    if blocked:
        raise RuntimeError(
            f"refusing to downgrade 0016_temporal_runtime: {blocked} agent_runs row(s) hold "
            f"{' or '.join(_UNREPRESENTABLE_STATUSES)}, which the pre-Temporal five-value "
            "status check cannot express. Resolve or archive those runs first; this "
            "migration will not map, delete or mislabel them."
        )

    # 6.
    op.drop_index("ix_agent_runs_claim", table_name="agent_runs")
    op.drop_index("ix_agent_runs_status", table_name="agent_runs")

    with op.batch_alter_table(
        "agent_runs",
        copy_from=_table(
            status_check=_NEW_STATUS_CHECK,
            identity_nullable=False,
            unique_workflow_id=True,
        ),
    ) as batch_op:
        # 5.
        batch_op.drop_constraint("uq_agent_runs_workflow_id", type_="unique")
        # 4.
        batch_op.drop_constraint("ck_agent_runs_status", type_="check")
        batch_op.create_check_constraint("ck_agent_runs_status", _OLD_STATUS_CHECK)
        # 3.
        batch_op.alter_column("workflow_type", existing_type=sa.Text(), nullable=True)
        batch_op.alter_column("workflow_id", existing_type=sa.Text(), nullable=True)
        # 2 and 1: dropping the columns discards the backfill with them.
        for column in reversed(_NEW_COLUMNS):
            batch_op.drop_column(column)
