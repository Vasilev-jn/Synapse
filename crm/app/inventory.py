"""Inventory domain logic for the local web MVP."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.config import EXPORTS_DIR
from app.deduplication import normalized_key
from app.models import (
    AccessoryDetail,
    CatalogAlias,
    CatalogEntry,
    Category,
    ConsoleDetail,
    GameDetail,
    Item,
    MarketListing,
    MarketListingMatch,
    utcnow,
)


ROOT_CATEGORY = "Игры, приставки и программы"
GAME_CATEGORY = "Игры для приставок"
CONSOLE_CATEGORY = "Игровые приставки"
ACCESSORY_CATEGORY = "Аксессуары"

ITEM_STATUSES = ["Есть", "Продан"]
SOURCES = ["Авито", "личная встреча", "знакомый", "другой источник"]
PLATFORMS = ["PlayStation 4", "PlayStation 5"]
LANGUAGES = ["Русский", "Английский", "Другой", "Не указано"]
DISC_SURFACES = ["Без заметных царапин", "Есть царапины", "Есть глубокая царапина"]
TEST_RESULTS = ["Работает", "Не работает"]
GAME_COMPLETENESS = ["Диск в коробке", "Только диск", "Полный комплект", "Другое"]
CONSOLE_MODELS = ["Fat", "Slim", "Pro"]
STORAGE_SIZES = ["500 ГБ", "1 ТБ", "2 ТБ", "другое"]
ACCESSORY_TYPES = ["Геймпад", "Зарядная станция", "Подставка или крепление", "Кабель", "Другой аксессуар"]
ORIGINALITY = ["Оригинальный", "Неоригинальный", "Неизвестно"]

SEEDED_GAMES: list[tuple[str, list[str]]] = [
    ("Ghost of Tsushima", ["Призрак Цусимы"]),
    ("Battlefield 4 Premium Edition", []),
    ("Hogwarts Legacy", ["Хогвартс Легаси", "Хогвартс. Наследие"]),
    ("Horizon Zero Dawn", ["Хорайзен Зеро Даун"]),
    ("Diablo III: Reaper of Souls - Ultimate Evil Edition", ["Diablo 3 Reaper of Souls"]),
    ("Call of Duty: Infinite Warfare", []),
    ("Call of Duty: Advanced Warfare", []),
    ("Call of Duty: Modern Warfare Remastered", []),
    ("Call of Duty: Black Ops III", ["Call of Duty Black Ops 3"]),
    ("LittleBigPlanet 3", ["Little Big Planet 3"]),
    ("Sniper Elite III", ["Sniper Elite 3"]),
]

SEEDED_CONSOLES: list[tuple[str, list[str]]] = [
    (
        "PlayStation 5 Disc",
        [
            "PS5 Disc",
            "PS5 с дисководом",
            "PlayStation 5 с дисководом",
            "пс5 с дисководом",
            "плойка 5 с дисководом",
            "PS5 fat disc",
            "PS5 стандартная",
        ],
    ),
    (
        "PlayStation 5 Digital",
        [
            "PS5 Digital",
            "PS5 диджитал",
            "PlayStation 5 Digital Edition",
            "пс5 digital",
            "пс5 диджитал",
            "плойка 5 без дисковода",
            "PS5 без дисковода",
            "PlayStation 5 без дисковода",
        ],
    ),
    (
        "PlayStation 5 Slim Disc",
        [
            "PS5 Slim Disc",
            "PS5 Slim с дисководом",
            "PlayStation 5 Slim с дисководом",
            "пс5 слим с дисководом",
            "плойка 5 слим дисковод",
            "PS5 slim disk",
        ],
    ),
    (
        "PlayStation 5 Slim Digital",
        [
            "PS5 Slim Digital",
            "PS5 Slim диджитал",
            "PlayStation 5 Slim Digital Edition",
            "пс5 слим digital",
            "пс5 слим диджитал",
            "PS5 Slim без дисковода",
            "PlayStation 5 Slim без дисковода",
        ],
    ),
]


@dataclass
class ItemInput:
    category_id: int
    catalog_entry_id: int | None = None
    catalog_name: str | None = None
    quantity: int = 1
    purchased_at: datetime | str | None = None
    source: str | None = None
    source_url: str | None = None
    calculated_cost: float | None = None
    expected_sale_price: float | None = None
    comment: str | None = None
    platform: str | None = None
    interface_language: str | None = None
    subtitles_language: str | None = None
    voice_language: str | None = None
    disc_surface: str | None = None
    test_result: str | None = None
    completeness: str | None = None
    completeness_comment: str | None = None
    model: str | None = None
    storage_size: str | None = None
    controllers_count: int | None = None
    accessory_type: str | None = None
    originality: str | None = None
    condition: str | None = None
    detail_comment: str | None = None


@dataclass
class DashboardStats:
    available_items: int
    sold_items: int
    invested_total: float
    stock_cost_total: float
    revenue_total: float
    realized_profit: float | None
    market_listings_count: int
    matched_market_count: int


def seed_database(session: Session) -> None:
    root = ensure_category(session, ROOT_CATEGORY, parent_id=None, is_system=True)
    games = ensure_category(session, GAME_CATEGORY, parent_id=root.id, is_system=True)
    consoles = ensure_category(session, CONSOLE_CATEGORY, parent_id=root.id, is_system=True)
    ensure_category(session, ACCESSORY_CATEGORY, parent_id=root.id, is_system=True)

    for name, aliases in SEEDED_GAMES:
        entry = ensure_catalog_entry(session, category_id=games.id, name=name, is_system=True)
        for alias in aliases[:2]:
            ensure_alias(session, entry, alias)
    for name, aliases in SEEDED_CONSOLES:
        entry = ensure_catalog_entry(session, category_id=consoles.id, name=name, is_system=True)
        for alias in aliases:
            ensure_alias(session, entry, alias)


def ensure_category(session: Session, name: str, *, parent_id: int | None, is_system: bool = False) -> Category:
    clean_name = name.strip()
    category = session.scalar(
        select(Category).where(Category.name == clean_name, Category.parent_id.is_(parent_id))
        if parent_id is None
        else select(Category).where(Category.name == clean_name, Category.parent_id == parent_id)
    )
    if category is None:
        key = normalized_key(clean_name)
        candidates = session.scalars(
            select(Category).where(Category.parent_id.is_(parent_id))
            if parent_id is None
            else select(Category).where(Category.parent_id == parent_id)
        )
        category = next((candidate for candidate in candidates if normalized_key(candidate.name) == key), None)
    if category is None:
        category = Category(name=clean_name, parent_id=parent_id, is_system=is_system)
        session.add(category)
        session.flush()
    elif is_system and not category.is_system:
        category.is_system = True
    return category


def add_subcategory(session: Session, *, parent_id: int, name: str) -> Category:
    parent = session.get(Category, parent_id)
    if parent is None:
        raise ValueError("Родительская категория не найдена")
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Название категории обязательно")
    return ensure_category(session, clean_name, parent_id=parent_id, is_system=False)


def delete_category(session: Session, category_id: int) -> None:
    category = session.get(Category, category_id)
    if category is None:
        return
    if category.is_system:
        raise ValueError("Системную категорию нельзя удалить")
    child_count = session.scalar(select(func.count(Category.id)).where(Category.parent_id == category_id)) or 0
    item_count = session.scalar(select(func.count(Item.id)).where(Item.category_id == category_id)) or 0
    if child_count or item_count:
        raise ValueError("Категорию можно удалить только без товаров и подкатегорий")
    session.delete(category)


def ensure_catalog_entry(session: Session, *, category_id: int, name: str, is_system: bool = False) -> CatalogEntry:
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Название каталожной позиции обязательно")
    entry = session.scalar(select(CatalogEntry).where(CatalogEntry.category_id == category_id, CatalogEntry.name == clean_name))
    if entry is None:
        key = normalized_key(clean_name)
        entries = session.scalars(select(CatalogEntry).where(CatalogEntry.category_id == category_id))
        entry = next((candidate for candidate in entries if normalized_key(candidate.name) == key), None)
    if entry is None:
        entry = CatalogEntry(category_id=category_id, name=clean_name, is_system=is_system)
        session.add(entry)
        session.flush()
    elif is_system and not entry.is_system:
        entry.is_system = True
    return entry


def ensure_alias(session: Session, entry: CatalogEntry, alias: str) -> CatalogAlias | None:
    clean_alias = alias.strip()
    if not clean_alias:
        return None
    session.flush()
    existing = session.scalar(
        select(CatalogAlias).where(CatalogAlias.catalog_entry_id == entry.id, CatalogAlias.name == clean_alias)
    )
    if existing is not None:
        return existing
    key = normalized_key(clean_alias)
    for candidate in session.scalars(select(CatalogAlias).where(CatalogAlias.catalog_entry_id == entry.id)):
        if normalized_key(candidate.name) == key:
            return candidate
    alias_count = session.scalar(select(func.count(CatalogAlias.id)).where(CatalogAlias.catalog_entry_id == entry.id)) or 0
    if alias_count >= 50:
        raise ValueError("У каталожной позиции может быть максимум 50 альтернативных названий")
    created = CatalogAlias(catalog_entry_id=entry.id, name=clean_alias)
    session.add(created)
    return created


def merge_catalog_entries(session: Session, *, source_id: int, target_id: int) -> None:
    if source_id == target_id:
        raise ValueError("Нельзя объединить запись саму с собой")
    source = session.get(CatalogEntry, source_id)
    target = session.get(CatalogEntry, target_id)
    if source is None or target is None:
        raise ValueError("Каталожная позиция не найдена")
    for item in session.scalars(select(Item).where(Item.catalog_entry_id == source_id)):
        item.catalog_entry_id = target_id
    for match in session.scalars(select(MarketListingMatch).where(MarketListingMatch.catalog_entry_id == source_id)):
        match.catalog_entry_id = target_id
    for alias in list(source.aliases):
        if len(target.aliases) < 2:
            ensure_alias(session, target, alias.name)
    session.delete(source)


def create_item(
    session: Session,
    *,
    item_input: ItemInput,
    default_cost: float | None = None,
) -> Item:
    category = session.get(Category, item_input.category_id)
    if category is None:
        raise ValueError("Категория товара не найдена")
    catalog_entry_id = item_input.catalog_entry_id
    catalog_name = clean_optional(item_input.catalog_name)
    if catalog_entry_id is None and not catalog_name:
        raise ValueError("Название товара обязательно")
    if catalog_name and catalog_name.casefold() == "без названия":
        raise ValueError("Укажите реальное название товара")
    if catalog_entry_id is None and catalog_name:
        catalog_entry = ensure_catalog_entry(session, category_id=item_input.category_id, name=catalog_name)
        catalog_entry_id = catalog_entry.id

    cost = item_input.calculated_cost if item_input.calculated_cost is not None else default_cost
    item = Item(
        category_id=item_input.category_id,
        catalog_entry_id=catalog_entry_id,
        status="Есть",
        purchased_at=parse_date(item_input.purchased_at),
        source=clean_optional(item_input.source),
        source_url=clean_optional(item_input.source_url),
        calculated_cost=cost,
        expected_sale_price=item_input.expected_sale_price,
        comment=clean_optional(item_input.comment),
    )
    session.add(item)
    session.flush()
    item.code = f"ITEM-{item.id:06d}"
    add_detail_for_item(session, item=item, category=category, item_input=item_input)
    return item


def add_detail_for_item(session: Session, *, item: Item, category: Category, item_input: ItemInput) -> None:
    root_name = top_level_category_name(category)
    if root_name == GAME_CATEGORY or category.name == GAME_CATEGORY:
        session.add(
            GameDetail(
                item_id=item.id,
                platform=clean_optional(item_input.platform),
                interface_language=clean_optional(item_input.interface_language),
                subtitles_language=clean_optional(item_input.subtitles_language),
                voice_language=clean_optional(item_input.voice_language),
                disc_surface=clean_optional(item_input.disc_surface),
                test_result=clean_optional(item_input.test_result),
                completeness=clean_optional(item_input.completeness),
                completeness_comment=clean_optional(item_input.completeness_comment),
            )
        )
    elif root_name == CONSOLE_CATEGORY or category.name == CONSOLE_CATEGORY:
        session.add(
            ConsoleDetail(
                item_id=item.id,
                model=clean_optional(item_input.model or item_input.catalog_name),
                storage_size=clean_optional(item_input.storage_size),
                controllers_count=item_input.controllers_count,
                completeness=clean_optional(item_input.completeness),
                condition=clean_optional(item_input.condition),
                comment=clean_optional(item_input.detail_comment),
            )
        )
    else:
        session.add(
            AccessoryDetail(
                item_id=item.id,
                accessory_type=clean_optional(item_input.accessory_type or item_input.catalog_name),
                platform=clean_optional(item_input.platform),
                originality=clean_optional(item_input.originality),
                condition=clean_optional(item_input.condition),
                comment=clean_optional(item_input.detail_comment),
            )
        )


def sell_item(
    session: Session,
    *,
    item_id: int,
    sale_price: float,
    sold_at: datetime | None,
    comment: str | None = None,
) -> Item:
    item = session.get(Item, item_id)
    if item is None:
        raise ValueError("Товар не найден")
    item.status = "Продан"
    item.sale_price = sale_price
    item.sold_at = sold_at or utcnow()
    item.is_synced = False
    if comment:
        item.comment = comment
    return item


def set_item_available(session: Session, item_id: int, *, clear_sale_fields: bool = True) -> Item:
    item = session.get(Item, item_id)
    if item is None:
        raise ValueError("Товар не найден")
    item.status = "Есть"
    item.is_synced = False
    if clear_sale_fields:
        item.sale_price = None
        item.sold_at = None
    return item


def item_profit(item: Item) -> float | None:
    if item.sale_price is None or item.calculated_cost is None:
        return None
    return item.sale_price - item.calculated_cost


def dashboard_stats(session: Session) -> DashboardStats:
    available_items = session.scalar(select(func.count(Item.id)).where(Item.status == "Есть")) or 0
    sold_items = session.scalar(select(func.count(Item.id)).where(Item.status == "Продан")) or 0
    invested_total = session.scalar(select(func.coalesce(func.sum(Item.calculated_cost), 0.0))) or 0.0
    stock_cost_total = session.scalar(select(func.coalesce(func.sum(Item.calculated_cost), 0.0)).where(Item.status == "Есть")) or 0.0
    revenue_total = session.scalar(select(func.coalesce(func.sum(Item.sale_price), 0.0)).where(Item.status == "Продан")) or 0.0
    sold_costs = list(session.scalars(select(Item).where(Item.status == "Продан")))
    known_profits = [item_profit(item) for item in sold_costs if item_profit(item) is not None]
    realized_profit = sum(known_profits) if known_profits else None
    market_listings_count = session.scalar(select(func.count(MarketListing.id))) or 0
    matched_market_count = session.scalar(
        select(func.count(MarketListingMatch.id)).where(MarketListingMatch.status.in_(("Автоматически", "Подтверждено")))
    ) or 0
    return DashboardStats(
        available_items=int(available_items),
        sold_items=int(sold_items),
        invested_total=float(invested_total),
        stock_cost_total=float(stock_cost_total),
        revenue_total=float(revenue_total),
        realized_profit=float(realized_profit) if realized_profit is not None else None,
        market_listings_count=int(market_listings_count),
        matched_market_count=int(matched_market_count),
    )


def get_items_query(session: Session):
    return session.scalars(
        select(Item)
        .options(
            joinedload(Item.category),
            joinedload(Item.catalog_entry),
            joinedload(Item.game_detail),
            joinedload(Item.console_detail),
            joinedload(Item.accessory_detail),
        )
        .order_by(Item.id.desc())
    )


def item_display_name(item: Item) -> str:
    if item.catalog_entry:
        return item.catalog_entry.name
    if item.console_detail and item.console_detail.model:
        return item.console_detail.model
    if item.accessory_detail and item.accessory_detail.accessory_type:
        return item.accessory_detail.accessory_type
    return "Без названия"


def item_platform_or_model(item: Item) -> str:
    if item.game_detail and item.game_detail.platform:
        return item.game_detail.platform
    if item.console_detail and item.console_detail.model:
        base_name = item.catalog_entry.name if item.catalog_entry else ""
        return f"{base_name} {item.console_detail.model}".strip()
    if item.accessory_detail and item.accessory_detail.platform:
        return item.accessory_detail.platform
    return ""


def item_cost_label(value: float | None) -> float | str | None:
    if value == 0:
        return "Бесплатно"
    return value


def export_items_csv(session: Session, path: Path | None = None) -> Path:
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = path or EXPORTS_DIR / f"items_{utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    rows = list(get_items_query(session))
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "код",
                "категория",
                "название",
                "платформа или модель",
                "статус",
                "дата покупки",
                "источник",
                "ссылка источника",
                "расчётная себестоимость",
                "ожидаемая цена",
                "цена продажи",
                "дата продажи",
                "прибыль",
                "комментарий",
            ],
        )
        writer.writeheader()
        for item in rows:
            writer.writerow(
                {
                    "код": item.code,
                    "категория": item.category.name,
                    "название": item_display_name(item),
                    "платформа или модель": item_platform_or_model(item),
                    "статус": item.status,
                    "дата покупки": item.purchased_at.date().isoformat() if item.purchased_at else "",
                    "источник": item.source,
                    "ссылка источника": item.source_url,
                    "расчётная себестоимость": item_cost_label(item.calculated_cost),
                    "ожидаемая цена": item.expected_sale_price,
                    "цена продажи": item.sale_price,
                    "дата продажи": item.sold_at.date().isoformat() if item.sold_at else "",
                    "прибыль": item_profit(item),
                    "комментарий": item.comment,
                }
            )
    return path


def top_level_category_name(category: Category) -> str:
    current = category
    while current.parent and current.parent.name != ROOT_CATEGORY:
        current = current.parent
    return current.name


def parse_date(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        try:
            return datetime.strptime(str(value), "%Y-%m-%d")
        except ValueError:
            return None


def to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None

