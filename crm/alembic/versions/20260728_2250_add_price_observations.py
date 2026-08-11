"""Add price observations.

Revision ID: 20260728_2250
Revises: 20260728_2100
Create Date: 2026-07-28 22:50:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260728_2250"
down_revision = "20260728_2100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "price_observations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("source_uid", sa.String(length=255), nullable=False),
        sa.Column("observation_type", sa.String(length=80), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=True),
        sa.Column("analysis_item_id", sa.Integer(), nullable=True),
        sa.Column("market_listing_id", sa.Integer(), nullable=True),
        sa.Column("catalog_entry_id", sa.Integer(), nullable=True),
        sa.Column("item_title", sa.Text(), nullable=False),
        sa.Column("platform_or_model", sa.String(length=255), nullable=True),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("delivery_price", sa.Float(), nullable=True),
        sa.Column("price_with_delivery", sa.Float(), nullable=True),
        sa.Column("currency", sa.String(length=20), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("reference_url", sa.Text(), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["analysis_item_id"], ["analysis_items.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["catalog_entry_id"], ["catalog_entries.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["market_listing_id"], ["market_listings.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "source_uid", name="uq_price_observation_source_uid"),
    )
    op.create_index(op.f("ix_price_observations_analysis_item_id"), "price_observations", ["analysis_item_id"])
    op.create_index(op.f("ix_price_observations_catalog_entry_id"), "price_observations", ["catalog_entry_id"])
    op.create_index(op.f("ix_price_observations_item_id"), "price_observations", ["item_id"])
    op.create_index(op.f("ix_price_observations_market_listing_id"), "price_observations", ["market_listing_id"])
    op.create_index(op.f("ix_price_observations_observation_type"), "price_observations", ["observation_type"])
    op.create_index(op.f("ix_price_observations_observed_at"), "price_observations", ["observed_at"])
    op.create_index(op.f("ix_price_observations_source"), "price_observations", ["source"])
    op.create_index(op.f("ix_price_observations_source_uid"), "price_observations", ["source_uid"])


def downgrade() -> None:
    op.drop_index(op.f("ix_price_observations_source_uid"), table_name="price_observations")
    op.drop_index(op.f("ix_price_observations_source"), table_name="price_observations")
    op.drop_index(op.f("ix_price_observations_observed_at"), table_name="price_observations")
    op.drop_index(op.f("ix_price_observations_observation_type"), table_name="price_observations")
    op.drop_index(op.f("ix_price_observations_market_listing_id"), table_name="price_observations")
    op.drop_index(op.f("ix_price_observations_item_id"), table_name="price_observations")
    op.drop_index(op.f("ix_price_observations_catalog_entry_id"), table_name="price_observations")
    op.drop_index(op.f("ix_price_observations_analysis_item_id"), table_name="price_observations")
    op.drop_table("price_observations")
