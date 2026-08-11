"""Ensure legacy Apify parser tables are absent.

Revision ID: 20260726_2230
Revises: 20260726_2218
Create Date: 2026-07-26 22:30:00
"""

from __future__ import annotations

from alembic import op


revision = "20260726_2230"
down_revision = "20260726_2218"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS raw_listings")
    op.execute("DROP TABLE IF EXISTS scrape_runs")
    op.execute("DROP TABLE IF EXISTS listings")
    op.execute("DROP TABLE IF EXISTS collections")


def downgrade() -> None:
    pass
