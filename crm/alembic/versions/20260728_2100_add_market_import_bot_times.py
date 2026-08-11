"""Add bot timing fields to market imports.

Revision ID: 20260728_2100
Revises: 20260726_2230
Create Date: 2026-07-28 21:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260728_2100"
down_revision = "20260726_2230"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("market_imports", sa.Column("scraped_at", sa.DateTime(), nullable=True))
    op.add_column("market_imports", sa.Column("crm_sent_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("market_imports", "crm_sent_at")
    op.drop_column("market_imports", "scraped_at")
