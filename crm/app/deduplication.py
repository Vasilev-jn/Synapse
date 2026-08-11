"""Conservative technical deduplication for CRM tables."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import (
    AnalysisItem,
    CatalogAlias,
    CatalogEntry,
    Category,
    Item,
    MarketListingMatch,
)


@dataclass
class DedupStats:
    categories_merged: int = 0
    catalog_entries_merged: int = 0
    aliases_removed: int = 0
    market_matches_removed: int = 0
    analysis_items_merged: int = 0
    item_codes_fixed: int = 0

    @property
    def total_changes(self) -> int:
        return (
            self.categories_merged
            + self.catalog_entries_merged
            + self.aliases_removed
            + self.market_matches_removed
            + self.analysis_items_merged
            + self.item_codes_fixed
        )


def normalized_key(value: Any) -> str:
    text = str(value or "").casefold().replace("ё", "е")
    return re.sub(r"[^0-9a-zа-я]+", "", text)


def deduplicate_all(session: Session) -> DedupStats:
    stats = DedupStats()
    stats.categories_merged = deduplicate_categories(session)
    stats.catalog_entries_merged = deduplicate_catalog_entries(session)
    stats.aliases_removed = deduplicate_catalog_aliases(session)
    stats.market_matches_removed = deduplicate_market_matches(session)
    stats.analysis_items_merged = deduplicate_analysis_items(session)
    stats.item_codes_fixed = fix_missing_item_codes(session)
    session.flush()
    return stats


def deduplicate_categories(session: Session) -> int:
    merged = 0
    while True:
        rows = list(session.scalars(select(Category).order_by(Category.parent_id, Category.id)))
        duplicate = first_duplicate(rows, lambda row: (row.parent_id or 0, normalized_key(row.name)))
        if duplicate is None:
            return merged
        target, source = duplicate
        for child in session.scalars(select(Category).where(Category.parent_id == source.id)):
            child.parent_id = target.id
        for entry in session.scalars(select(CatalogEntry).where(CatalogEntry.category_id == source.id)):
            entry.category_id = target.id
        for item in session.scalars(select(Item).where(Item.category_id == source.id)):
            item.category_id = target.id
        if source.is_system and not target.is_system:
            target.is_system = True
        session.delete(source)
        session.flush()
        merged += 1


def deduplicate_catalog_entries(session: Session) -> int:
    merged = 0
    while True:
        rows = list(session.scalars(select(CatalogEntry).order_by(CatalogEntry.category_id, CatalogEntry.id)))
        duplicate = first_duplicate(rows, lambda row: (row.category_id, normalized_key(row.name)))
        if duplicate is None:
            return merged
        target, source = duplicate
        for item in session.scalars(select(Item).where(Item.catalog_entry_id == source.id)):
            item.catalog_entry_id = target.id
        for alias in session.scalars(select(CatalogAlias).where(CatalogAlias.catalog_entry_id == source.id)):
            if not catalog_alias_exists(session, target.id, alias.name):
                alias.catalog_entry_id = target.id
            else:
                session.delete(alias)
        if source.is_system and not target.is_system:
            target.is_system = True
        session.delete(source)
        session.flush()
        merged += 1


def deduplicate_catalog_aliases(session: Session) -> int:
    removed = 0
    rows = list(session.scalars(select(CatalogAlias).order_by(CatalogAlias.catalog_entry_id, CatalogAlias.id)))
    seen: set[tuple[int, str]] = set()
    for alias in rows:
        key = (alias.catalog_entry_id, normalized_key(alias.name))
        if not key[1] or key in seen:
            session.delete(alias)
            removed += 1
            continue
        seen.add(key)
    session.flush()
    return removed


def deduplicate_market_matches(session: Session) -> int:
    removed = 0
    rows = list(session.scalars(select(MarketListingMatch).order_by(MarketListingMatch.market_listing_id, MarketListingMatch.id.desc())))
    keep_by_listing: dict[int, MarketListingMatch] = {}
    for match in rows:
        current = keep_by_listing.get(match.market_listing_id)
        if current is None:
            keep_by_listing[match.market_listing_id] = match
            continue
        if match.status in {"Подтверждено", "Исключено"} and current.status not in {"Подтверждено", "Исключено"}:
            session.delete(current)
            keep_by_listing[match.market_listing_id] = match
        else:
            session.delete(match)
        removed += 1
    session.flush()
    return removed


def deduplicate_analysis_items(session: Session) -> int:
    merged = 0
    while True:
        rows = list(session.scalars(select(AnalysisItem).order_by(AnalysisItem.source_key, AnalysisItem.source_item_code, AnalysisItem.id)))
        duplicate = first_duplicate(rows, lambda row: (row.source_key, row.source_item_code))
        if duplicate is None:
            return merged
        target, source = duplicate
        merge_analysis_item(target, source)
        session.delete(source)
        session.flush()
        merged += 1


def fix_missing_item_codes(session: Session) -> int:
    changed = 0
    used = {str(code) for code in session.scalars(select(Item.code).where(Item.code.is_not(None))) if code}
    for item in session.scalars(select(Item).where(Item.code.is_(None)).order_by(Item.id)):
        code = f"ITEM-{item.id:06d}"
        suffix = 1
        while code in used:
            suffix += 1
            code = f"ITEM-{item.id:06d}-{suffix}"
        item.code = code
        used.add(code)
        changed += 1
    return changed


def first_duplicate(rows: list[Any], key_func: Any) -> tuple[Any, Any] | None:
    seen: dict[Any, Any] = {}
    for row in rows:
        key = key_func(row)
        if not key or (isinstance(key, tuple) and any(part == "" for part in key)):
            continue
        target = seen.get(key)
        if target is not None:
            return target, row
        seen[key] = row
    return None


def catalog_alias_exists(session: Session, catalog_entry_id: int, name: str) -> bool:
    key = normalized_key(name)
    for alias in session.scalars(select(CatalogAlias).where(CatalogAlias.catalog_entry_id == catalog_entry_id)):
        if normalized_key(alias.name) == key:
            return True
    return False


def merge_analysis_item(target: AnalysisItem, source: AnalysisItem) -> None:
    for attr in (
        "source_name",
        "source_item_id",
        "category",
        "platform_or_model",
        "expected_sale_price",
        "purchased_at",
        "sold_at",
        "details",
        "last_import_id",
    ):
        if getattr(target, attr) in (None, "", {}):
            setattr(target, attr, getattr(source, attr))
    for attr in ("calculated_cost", "sale_price", "profit"):
        if getattr(source, attr) is not None:
            setattr(target, attr, getattr(source, attr))
    if source.status == "Продан":
        target.status = source.status
    if len(source.title or "") > len(target.title or ""):
        target.title = source.title

