"""Drop legacy Apify parser tables.

Revision ID: 20260726_2218
Revises: 20260726_2204
Create Date: 2026-07-26 22:18:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260726_2218"
down_revision = "20260726_2204"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("raw_listings")
    op.drop_table("scrape_runs")
    op.drop_table("listings")
    op.drop_table("collections")


def downgrade() -> None:
    op.create_table(
        "collections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=False),
        sa.Column("search_url", sa.Text(), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("target_results", sa.Integer(), nullable=True),
        sa.Column("max_runs", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_collections_category", "collections", ["category"])
    op.create_index("ix_collections_status", "collections", ["status"])

    op.create_table(
        "listings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("external_id", sa.String(length=100), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("price", sa.Integer(), nullable=True),
        sa.Column("currency", sa.String(length=20), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("posted_at", sa.DateTime(), nullable=True),
        sa.Column("scraped_at", sa.DateTime(), nullable=True),
        sa.Column("seller_user_key", sa.String(length=255), nullable=True),
        sa.Column("platform", sa.String(length=255), nullable=True),
        sa.Column("listing_category_name", sa.String(length=255), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("raw_latest_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_listings_category", "listings", ["category"])
    op.create_index("ix_listings_external_id", "listings", ["external_id"], unique=True)

    op.create_table(
        "scrape_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("collection_id", sa.Integer(), sa.ForeignKey("collections.id"), nullable=True),
        sa.Column("actor_id", sa.String(length=255), nullable=True),
        sa.Column("apify_run_id", sa.String(length=255), nullable=True),
        sa.Column("dataset_id", sa.String(length=255), nullable=True),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("min_price", sa.Integer(), nullable=True),
        sa.Column("max_price", sa.Integer(), nullable=True),
        sa.Column("input_json", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("received_count", sa.Integer(), nullable=False),
        sa.Column("unique_on_page_count", sa.Integer(), nullable=False),
        sa.Column("new_unique_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_count", sa.Integer(), nullable=False),
        sa.Column("overlap_percent", sa.Float(), nullable=False),
        sa.Column("estimated_cost_usd", sa.Float(), nullable=True),
        sa.Column("actual_cost_usd", sa.Float(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("collection_id", "page_number", name="uq_collection_page"),
        sa.UniqueConstraint("collection_id", "min_price", "max_price", name="uq_collection_price_range"),
    )
    op.create_index("ix_scrape_runs_apify_run_id", "scrape_runs", ["apify_run_id"])
    op.create_index("ix_scrape_runs_status", "scrape_runs", ["status"])

    op.create_table(
        "raw_listings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("scrape_run_id", sa.Integer(), sa.ForeignKey("scrape_runs.id"), nullable=True),
        sa.Column("external_id", sa.String(length=100), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=False),
        sa.Column("scraped_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_raw_listings_external_id", "raw_listings", ["external_id"])
