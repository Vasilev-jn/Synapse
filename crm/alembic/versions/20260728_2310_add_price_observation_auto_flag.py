"""Add auto-price usability flag to price observations.

Revision ID: 20260728_2310
Revises: 20260728_2250
Create Date: 2026-07-28 23:10:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260728_2310"
down_revision = "20260728_2250"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "price_observations",
        sa.Column("usable_for_auto_price", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        op.f("ix_price_observations_usable_for_auto_price"),
        "price_observations",
        ["usable_for_auto_price"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_price_observations_usable_for_auto_price"), table_name="price_observations")
    op.drop_column("price_observations", "usable_for_auto_price")
