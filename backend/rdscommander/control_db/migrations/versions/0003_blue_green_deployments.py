"""blue green deployments

Adds the table that follows each RDS blue/green deployment a major upgrade creates, from creation
through switchover or deletion.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-12 09:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "blue_green_deployments",
        sa.Column(
            "deployment_id",
            sa.String(length=64),
            nullable=False,
            comment="AWS blue/green deployment identifier, e.g. bgd-0123456789abcdef",
        ),
        sa.Column("operation_id", sa.UUID(), nullable=False, comment="Operation that created the deployment"),
        sa.Column(
            "resource_arn",
            sa.String(length=300),
            nullable=False,
            comment="ARN of the blue (source) instance or cluster",
        ),
        sa.Column(
            "environment_slug",
            sa.String(length=40),
            nullable=False,
            comment="Environment of the source resource",
        ),
        sa.Column(
            "target_key",
            sa.String(length=80),
            nullable=False,
            comment="Target (account and region) it lives in",
        ),
        sa.Column(
            "status",
            sa.String(length=40),
            nullable=False,
            comment=(
                "Last status read from AWS: PROVISIONING AVAILABLE SWITCHOVER_IN_PROGRESS "
                "SWITCHOVER_COMPLETED SWITCHOVER_FAILED INVALID_CONFIGURATION DELETING"
            ),
        ),
        sa.Column(
            "green_endpoint",
            sa.String(length=300),
            nullable=True,
            comment="Endpoint address of the green side, once AWS reports one",
        ),
        sa.Column(
            "target_engine_version",
            sa.String(length=32),
            nullable=False,
            comment="Engine version the green side runs",
        ),
        sa.Column(
            "created_at_utc",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="When the console recorded the deployment",
        ),
        sa.Column(
            "updated_at_utc",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="When the status was last updated",
        ),
        sa.Column(
            "switched_over_at_utc",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the switchover was observed complete",
        ),
        sa.Column(
            "deleted_at_utc",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the deployment was deleted in AWS",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["operations.id"],
        ),
        sa.PrimaryKeyConstraint("deployment_id"),
        comment="Blue/green deployments of major upgrades, from creation to switchover or deletion",
    )
    op.create_index(
        op.f("ix_blue_green_deployments_operation_id"),
        "blue_green_deployments",
        ["operation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_blue_green_deployments_resource_arn"),
        "blue_green_deployments",
        ["resource_arn"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_blue_green_deployments_resource_arn"), table_name="blue_green_deployments")
    op.drop_index(op.f("ix_blue_green_deployments_operation_id"), table_name="blue_green_deployments")
    op.drop_table("blue_green_deployments")
