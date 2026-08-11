"""SQLAlchemy models for CRM inventory, analytics, and market data."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("categories.id"), nullable=True, index=True)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    parent: Mapped[Optional["Category"]] = relationship(remote_side=[id], back_populates="children")
    children: Mapped[list["Category"]] = relationship(back_populates="parent")
    catalog_entries: Mapped[list["CatalogEntry"]] = relationship(back_populates="category")
    items: Mapped[list["Item"]] = relationship(back_populates="category")

    __table_args__ = (UniqueConstraint("parent_id", "name", name="uq_category_parent_name"),)


class CatalogEntry(Base):
    __tablename__ = "catalog_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"), index=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    category: Mapped[Category] = relationship(back_populates="catalog_entries")
    aliases: Mapped[list["CatalogAlias"]] = relationship(
        back_populates="catalog_entry",
        cascade="all, delete-orphan",
        order_by="CatalogAlias.id",
    )
    items: Mapped[list["Item"]] = relationship(back_populates="catalog_entry")

    __table_args__ = (UniqueConstraint("category_id", "name", name="uq_catalog_category_name"),)


class CatalogAlias(Base):
    __tablename__ = "catalog_aliases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    catalog_entry_id: Mapped[int] = mapped_column(ForeignKey("catalog_entries.id"), index=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    catalog_entry: Mapped[CatalogEntry] = relationship(back_populates="aliases")

    __table_args__ = (UniqueConstraint("catalog_entry_id", "name", name="uq_alias_entry_name"),)


class Item(Base):
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[Optional[str]] = mapped_column(String(20), unique=True, nullable=True, index=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"), index=True)
    catalog_entry_id: Mapped[Optional[int]] = mapped_column(ForeignKey("catalog_entries.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="Есть", index=True)
    purchased_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    source_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    calculated_cost: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    expected_sale_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sale_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sold_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_synced: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    category: Mapped[Category] = relationship(back_populates="items")
    catalog_entry: Mapped[Optional[CatalogEntry]] = relationship(back_populates="items")
    game_detail: Mapped[Optional["GameDetail"]] = relationship(
        back_populates="item",
        cascade="all, delete-orphan",
        uselist=False,
    )
    console_detail: Mapped[Optional["ConsoleDetail"]] = relationship(
        back_populates="item",
        cascade="all, delete-orphan",
        uselist=False,
    )
    accessory_detail: Mapped[Optional["AccessoryDetail"]] = relationship(
        back_populates="item",
        cascade="all, delete-orphan",
        uselist=False,
    )


class GameDetail(Base):
    __tablename__ = "game_details"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), unique=True, index=True)
    platform: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    interface_language: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    subtitles_language: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    voice_language: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    disc_surface: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    test_result: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    completeness: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    completeness_comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    item: Mapped[Item] = relationship(back_populates="game_detail")


class ConsoleDetail(Base):
    __tablename__ = "console_details"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), unique=True, index=True)
    model: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    storage_size: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    controllers_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    completeness: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    condition: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    item: Mapped[Item] = relationship(back_populates="console_detail")


class AccessoryDetail(Base):
    __tablename__ = "accessory_details"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), unique=True, index=True)
    accessory_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    platform: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    originality: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    condition: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    item: Mapped[Item] = relationship(back_populates="accessory_detail")


class MarketImport(Base):
    __tablename__ = "market_imports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(Text)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    scraped_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    crm_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    raw_count: Mapped[int] = mapped_column(Integer, default=0)
    unique_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)


class AnalysisImport(Base):
    __tablename__ = "analysis_imports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    export_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    source_key: Mapped[str] = mapped_column(String(120), index=True)
    source_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    filename: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    exported_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    sold_count: Mapped[int] = mapped_column(Integer, default=0)
    created_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, default=0)

    items: Mapped[list["AnalysisItem"]] = relationship(back_populates="last_import")


class AnalysisItem(Base):
    __tablename__ = "analysis_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_import_id: Mapped[Optional[int]] = mapped_column(ForeignKey("analysis_imports.id"), nullable=True, index=True)
    source_key: Mapped[str] = mapped_column(String(120), index=True)
    source_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    source_item_code: Mapped[str] = mapped_column(String(80), index=True)
    source_item_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(255), index=True)
    platform_or_model: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    calculated_cost: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    expected_sale_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sale_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    profit: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    purchased_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    sold_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    last_import: Mapped[Optional[AnalysisImport]] = relationship(back_populates="items")

    __table_args__ = (UniqueConstraint("source_key", "source_item_code", name="uq_analysis_source_item"),)


class MarketListing(Base):
    __tablename__ = "market_listings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    bot_listing_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    currency: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    platform: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    format: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, index=True)
    type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, index=True)
    localization: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    seller_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    seller_user_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    seller_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, index=True)
    is_shop: Mapped[bool] = mapped_column(Boolean, default=False)
    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    scraped_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    exported_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)

    matches: Mapped[list["MarketListingMatch"]] = relationship(
        back_populates="market_listing",
        cascade="all, delete-orphan",
    )


class MarketListingMatch(Base):
    __tablename__ = "market_listing_matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_listing_id: Mapped[int] = mapped_column(ForeignKey("market_listings.id"), index=True)
    catalog_entry_id: Mapped[Optional[int]] = mapped_column(ForeignKey("catalog_entries.id"), nullable=True, index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(40), default="Неоднозначно", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    market_listing: Mapped[MarketListing] = relationship(back_populates="matches")
    catalog_entry: Mapped[Optional[CatalogEntry]] = relationship()


class PriceObservation(Base):
    __tablename__ = "price_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(80), index=True)
    source_uid: Mapped[str] = mapped_column(String(255), index=True)
    observation_type: Mapped[str] = mapped_column(String(80), index=True)
    item_id: Mapped[Optional[int]] = mapped_column(ForeignKey("items.id", ondelete="SET NULL"), nullable=True, index=True)
    analysis_item_id: Mapped[Optional[int]] = mapped_column(ForeignKey("analysis_items.id", ondelete="SET NULL"), nullable=True, index=True)
    market_listing_id: Mapped[Optional[int]] = mapped_column(ForeignKey("market_listings.id", ondelete="SET NULL"), nullable=True, index=True)
    catalog_entry_id: Mapped[Optional[int]] = mapped_column(ForeignKey("catalog_entries.id", ondelete="SET NULL"), nullable=True, index=True)
    item_title: Mapped[str] = mapped_column(Text)
    platform_or_model: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    price: Mapped[float] = mapped_column(Float)
    delivery_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    price_with_delivery: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(20), default="RUB")
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    usable_for_auto_price: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", index=True)
    reference_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    raw_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    item: Mapped[Optional[Item]] = relationship()
    analysis_item: Mapped[Optional[AnalysisItem]] = relationship()
    market_listing: Mapped[Optional[MarketListing]] = relationship()
    catalog_entry: Mapped[Optional[CatalogEntry]] = relationship()

    __table_args__ = (UniqueConstraint("source", "source_uid", name="uq_price_observation_source_uid"),)
