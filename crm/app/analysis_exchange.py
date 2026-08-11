"""Export and import CRM snapshots for shared analytics."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, selectinload

from app.config import CURRENT_EXPORT_PATH, METADATA_DIR, QUEUE_IMPORTS_DIR, Settings
from app.inventory import item_display_name, item_platform_or_model, item_profit, top_level_category_name
from app.market import import_avito_api_batch
from app.models import AnalysisImport, AnalysisItem, Item, MarketListing, PriceObservation, utcnow
from app.price_observations import record_partner_sale_observation


EXPORT_FORMAT = "crm-analysis-export"
EXPORT_VERSION = 2
EXPORT_JSON_NAME = "crm-analysis-export.json"


@dataclass
class ImportResult:
    import_record: AnalysisImport | None
    created_count: int
    updated_count: int
    market_created_count: int = 0
    market_duplicate_count: int = 0
    skipped: bool = False


@dataclass
class ExportQueueResult:
    path: Path | None
    item_ids: list[int]

    @property
    def exported_count(self) -> int:
        return len(self.item_ids)


@dataclass
class DownloadExportResult:
    payload: dict[str, Any]
    item_ids: list[int]
    market_listing_ids: list[int]

    @property
    def exported_count(self) -> int:
        return len(self.item_ids) + len(self.market_listing_ids)


@dataclass
class CrocTargetResult:
    user_id: str
    code: str
    return_code: int | None
    success: bool
    error: str | None = None


@dataclass
class CrocSendResult:
    path: Path
    targets: list[CrocTargetResult]
    synced_item_ids: list[int]

    @property
    def success_count(self) -> int:
        return sum(1 for target in self.targets if target.success)

    @property
    def completed(self) -> bool:
        return bool(self.synced_item_ids)


def build_incremental_analysis_export_file(db: Session, settings: Settings) -> ExportQueueResult:
    items = unsynced_export_items(db)
    if not items:
        CURRENT_EXPORT_PATH.unlink(missing_ok=True)
        return ExportQueueResult(path=None, item_ids=[])

    CURRENT_EXPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = build_analysis_export_payload(items, settings)
    item_ids = [item.id for item in items]
    payload["sync"] = {"item_ids": item_ids}
    CURRENT_EXPORT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return ExportQueueResult(path=CURRENT_EXPORT_PATH, item_ids=item_ids)


def build_download_export_payload(db: Session, settings: Settings) -> DownloadExportResult:
    """Build one downloadable export for personal inventory and bot listings.

    Personal inventory uses Item.is_synced. Bot Avito listings use
    MarketListing.exported_at. Nothing is sent through croc here; the browser
    downloads the JSON directly.
    """

    items = unsynced_export_items(db)
    market_listings = unsynced_market_listings(db)
    payload = build_combined_export_payload(items, market_listings, settings)
    return DownloadExportResult(
        payload=payload,
        item_ids=[item.id for item in items],
        market_listing_ids=[listing.id for listing in market_listings],
    )


def mark_download_exported(db: Session, result: DownloadExportResult) -> None:
    exported_at = utcnow()
    if result.item_ids:
        db.execute(update(Item).where(Item.id.in_(result.item_ids)).values(is_synced=True))
    if result.market_listing_ids:
        db.execute(update(MarketListing).where(MarketListing.id.in_(result.market_listing_ids)).values(exported_at=exported_at))


def unsynced_export_items(db: Session) -> list[Item]:
    return list(
        db.scalars(
            select(Item)
            .options(
                selectinload(Item.category),
                selectinload(Item.catalog_entry),
                selectinload(Item.game_detail),
                selectinload(Item.console_detail),
                selectinload(Item.accessory_detail),
            )
            .where(Item.is_synced.is_(False))
            .order_by(Item.id)
        )
    )


def unsynced_market_listings(db: Session) -> list[MarketListing]:
    return list(
        db.scalars(
            select(MarketListing)
            .where(MarketListing.exported_at.is_(None))
            .order_by(MarketListing.id)
        )
    )


def build_analysis_export_payload(items: list[Item], settings: Settings) -> dict[str, Any]:
    source = export_source(settings)
    exported_items = [item_export_payload(item) for item in items]
    return {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "export_id": uuid.uuid4().hex,
        "exported_at": utcnow().isoformat(),
        "source": source,
        "counts": {
            "items": len(exported_items),
            "sold": sum(1 for row in exported_items if row.get("status") == "Продан"),
        },
        "items": exported_items,
    }


def build_combined_export_payload(
    items: list[Item],
    market_listings: list[MarketListing],
    settings: Settings,
) -> dict[str, Any]:
    payload = build_analysis_export_payload(items, settings)
    exported_market = [market_listing_export_payload(listing) for listing in market_listings]
    payload["version"] = 2
    payload["export_kind"] = "inventory-and-avito-bot"
    payload["counts"].update(
        {
            "personal_items": len(payload["items"]),
            "market_listings": len(exported_market),
            "total_records": len(payload["items"]) + len(exported_market),
        }
    )
    payload["market_listings"] = exported_market
    payload["sync"] = {
        "item_ids": [item.id for item in items],
        "market_listing_ids": [listing.id for listing in market_listings],
    }
    return payload


def export_source(settings: Settings) -> dict[str, str]:
    source_id = (settings.my_id or settings.crm_export_source_id or "").strip() or persistent_source_id()
    source_name = (settings.crm_export_source_name or settings.my_id or "").strip() or os.getenv("COMPUTERNAME") or "CRM"
    return {"id": source_id, "name": source_name}


def persistent_source_id() -> str:
    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    path = METADATA_DIR / "crm_export_source_id.txt"
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    seed = "|".join([os.getenv("COMPUTERNAME", ""), os.getenv("USERNAME", ""), str(Path.cwd())])
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    value = f"crm-{digest}"
    path.write_text(value, encoding="utf-8")
    return value


def item_export_payload(item: Item) -> dict[str, Any]:
    return {
        "source_item_id": str(item.id),
        "code": item.code or f"item-{item.id}",
        "category": item.category.name if item.category else None,
        "root_category": top_level_category_name(item.category) if item.category else None,
        "title": item_display_name(item),
        "platform_or_model": item_platform_or_model(item),
        "status": item.status,
        "calculated_cost": item.calculated_cost,
        "expected_sale_price": item.expected_sale_price,
        "sale_price": item.sale_price,
        "profit": item_profit(item),
        "purchased_at": datetime_to_json(item.purchased_at),
        "source": item.source,
        "source_url": item.source_url,
        "sold_at": datetime_to_json(item.sold_at),
        "created_at": datetime_to_json(item.created_at),
        "updated_at": datetime_to_json(item.updated_at),
        "details": item_details_payload(item),
    }


def market_listing_export_payload(listing: MarketListing) -> dict[str, Any]:
    raw_json = listing.raw_json if isinstance(listing.raw_json, dict) else {}
    return {
        "source_listing_id": str(listing.id),
        "external_id": listing.external_id,
        "bot_listing_id": listing.bot_listing_id,
        "title": listing.title,
        "description": listing.description,
        "url": listing.url,
        "price": listing.price,
        "currency": listing.currency,
        "address": listing.address,
        "platform": listing.platform,
        "format": listing.format,
        "type": listing.type,
        "seller_name": listing.seller_name,
        "seller_user_key": listing.seller_user_key,
        "seller_type": listing.seller_type,
        "is_shop": listing.is_shop,
        "posted_at": datetime_to_json(listing.posted_at),
        "scraped_at": datetime_to_json(listing.scraped_at),
        "first_seen_at": datetime_to_json(listing.first_seen_at),
        "last_seen_at": datetime_to_json(listing.last_seen_at),
        "raw_json": raw_json,
    }


def item_details_payload(item: Item) -> dict[str, Any]:
    if item.game_detail:
        return {
            "kind": "game",
            "platform": item.game_detail.platform,
            "interface_language": item.game_detail.interface_language,
            "subtitles_language": item.game_detail.subtitles_language,
            "voice_language": item.game_detail.voice_language,
            "disc_surface": item.game_detail.disc_surface,
            "test_result": item.game_detail.test_result,
            "completeness": item.game_detail.completeness,
            "edition": item.game_detail.completeness_comment,
        }
    if item.console_detail:
        return {
            "kind": "console",
            "model": item.console_detail.model,
            "storage_size": item.console_detail.storage_size,
            "controllers_count": item.console_detail.controllers_count,
            "completeness": item.console_detail.completeness,
            "condition": item.console_detail.condition,
            "comment": item.console_detail.comment,
        }
    if item.accessory_detail:
        return {
            "kind": "accessory",
            "accessory_type": item.accessory_detail.accessory_type,
            "platform": item.accessory_detail.platform,
            "originality": item.accessory_detail.originality,
            "condition": item.accessory_detail.condition,
            "comment": item.accessory_detail.comment,
        }
    return {}


def import_analysis_export(db: Session, data: bytes, filename: str | None = None) -> ImportResult:
    payload = parse_analysis_export(data)
    export_id = str(payload.get("export_id") or "").strip()
    if not export_id:
        raise ValueError("В файле нет export_id.")
    existing_import = db.scalar(select(AnalysisImport).where(AnalysisImport.export_id == export_id))
    if existing_import is not None:
        return ImportResult(existing_import, 0, 0, skipped=True)

    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    source_key = str(source.get("id") or "").strip() or "unknown-source"
    source_name = str(source.get("name") or "").strip() or None
    raw_items = payload.get("items") if isinstance(payload.get("items"), list) else []
    raw_market_listings = payload.get("market_listings") if isinstance(payload.get("market_listings"), list) else []
    import_record = AnalysisImport(
        export_id=export_id,
        source_key=source_key,
        source_name=source_name,
        filename=filename,
        exported_at=parse_datetime(payload.get("exported_at")),
        item_count=len(raw_items) + len(raw_market_listings),
        sold_count=sum(1 for item in raw_items if isinstance(item, dict) and item.get("status") == "Продан"),
    )
    db.add(import_record)
    db.flush()

    created = 0
    updated = 0
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        source_item_code = clean_source_item_code(raw_item)
        existing = db.scalar(
            select(AnalysisItem).where(
                AnalysisItem.source_key == source_key,
                AnalysisItem.source_item_code == source_item_code,
            )
        )
        if existing is None:
            existing = AnalysisItem(
                source_key=source_key,
                source_name=source_name,
                source_item_code=source_item_code,
                title=clean_title(raw_item),
                status=str(raw_item.get("status") or "Есть"),
            )
            db.add(existing)
            created += 1
        else:
            updated += 1
        fill_analysis_item(existing, raw_item, import_record)
        db.flush()
        record_partner_sale_observation(db, existing)

    market_created = 0
    market_duplicates = 0
    if raw_market_listings:
        market_result = import_avito_api_batch(
            db,
            source=source_key,
            filename=filename or f"crm_export_{export_id}",
            scraped_at=parse_datetime(payload.get("exported_at")),
            crm_sent_at=None,
            listings=[
                imported_market_listing_payload(raw_listing)
                for raw_listing in raw_market_listings
                if isinstance(raw_listing, dict)
            ],
        )
        market_created = market_result.unique_count
        market_duplicates = market_result.duplicate_count

    import_record.created_count = created
    import_record.updated_count = updated
    return ImportResult(import_record, created, updated, market_created, market_duplicates)


def imported_market_listing_payload(raw_listing: dict[str, Any]) -> dict[str, Any]:
    raw_json = raw_listing.get("raw_json") if isinstance(raw_listing.get("raw_json"), dict) else {}
    source_listing_id = str(raw_listing.get("source_listing_id") or "").strip()
    export_raw_json = dict(raw_json)
    if source_listing_id and "source_listing_id" not in export_raw_json:
        export_raw_json["source_listing_id"] = source_listing_id
    return {
        "external_id": raw_listing.get("external_id") or source_listing_id or None,
        "bot_listing_id": raw_listing.get("bot_listing_id") or raw_listing.get("external_id") or source_listing_id or None,
        "title": raw_listing.get("title"),
        "description": raw_listing.get("description"),
        "url": raw_listing.get("url"),
        "price": raw_listing.get("price"),
        "currency": raw_listing.get("currency") or "RUB",
        "address": raw_listing.get("address"),
        "platform": raw_listing.get("platform"),
        "format": raw_listing.get("format"),
        "type": raw_listing.get("type"),
        "seller_name": raw_listing.get("seller_name"),
        "seller_user_key": raw_listing.get("seller_user_key"),
        "seller_type": raw_listing.get("seller_type"),
        "is_shop": raw_listing.get("is_shop"),
        "posted_at": raw_listing.get("posted_at"),
        "scraped_at": raw_listing.get("scraped_at") or raw_listing.get("last_seen_at") or raw_listing.get("first_seen_at"),
        "raw_json": export_raw_json,
    }


def parse_analysis_export(data: bytes) -> dict[str, Any]:
    if len(data) > 25 * 1024 * 1024:
        raise ValueError("Файл слишком большой для импорта.")
    try:
        payload = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Нужен JSON-файл экспорта CRM.") from exc
    if not isinstance(payload, dict) or payload.get("format") != EXPORT_FORMAT:
        raise ValueError("Это не файл экспорта аналитики CRM.")
    if int(payload.get("version") or 0) > EXPORT_VERSION:
        raise ValueError("Файл создан более новой версией CRM.")
    return payload


def clean_source_item_code(raw_item: dict[str, Any]) -> str:
    code = str(raw_item.get("code") or raw_item.get("source_item_id") or "").strip()
    if not code:
        code = uuid.uuid4().hex
    return code[:80]


def clean_title(raw_item: dict[str, Any]) -> str:
    value = str(raw_item.get("title") or "").strip()
    return value[:255] if value else "Без названия"


def fill_analysis_item(item: AnalysisItem, raw_item: dict[str, Any], import_record: AnalysisImport) -> None:
    item.last_import_id = import_record.id
    item.source_name = import_record.source_name
    item.source_item_id = str(raw_item.get("source_item_id") or "")[:80] or None
    item.category = str(raw_item.get("category") or "")[:255] or None
    item.title = clean_title(raw_item)
    item.platform_or_model = str(raw_item.get("platform_or_model") or "")[:255] or None
    item.status = str(raw_item.get("status") or "Есть")[:20]
    item.calculated_cost = safe_float(raw_item.get("calculated_cost"))
    item.expected_sale_price = safe_float(raw_item.get("expected_sale_price"))
    item.sale_price = safe_float(raw_item.get("sale_price"))
    item.profit = safe_float(raw_item.get("profit"))
    item.purchased_at = parse_datetime(raw_item.get("purchased_at"))
    item.sold_at = parse_datetime(raw_item.get("sold_at"))
    item.details = raw_item.get("details") if isinstance(raw_item.get("details"), dict) else None
    item.imported_at = import_record.imported_at


def analysis_sales_summary(db: Session, limit: int = 20) -> dict[str, Any]:
    rows = list(db.scalars(select(AnalysisItem).where(AnalysisItem.status == "Продан")))
    total_revenue = sum(row.sale_price or 0 for row in rows)
    known_profits = [row.profit for row in rows if row.profit is not None]
    by_title: dict[str, dict[str, Any]] = {}
    for row in rows:
        bucket = by_title.setdefault(
            row.title.casefold(),
            {
                "title": row.title,
                "category": row.category or "—",
                "count": 0,
                "revenue": 0.0,
                "profit": 0.0,
                "profit_count": 0,
                "sale_prices": [],
                "sources": set(),
            },
        )
        bucket["count"] += 1
        bucket["revenue"] += row.sale_price or 0
        if row.profit is not None:
            bucket["profit"] += row.profit
            bucket["profit_count"] += 1
        if row.sale_price is not None:
            bucket["sale_prices"].append(row.sale_price)
        if row.source_name:
            bucket["sources"].add(row.source_name)
    title_rows = sorted(by_title.values(), key=lambda item: (-item["count"], -item["revenue"], item["title"].casefold()))
    for row in title_rows:
        prices = row.pop("sale_prices")
        sources = row.pop("sources")
        row["average_price"] = sum(prices) / len(prices) if prices else None
        row["average_profit"] = row["profit"] / row["profit_count"] if row["profit_count"] else None
        row["sources_label"] = ", ".join(sorted(sources)) if sources else "импорт"
    return {
        "sold_count": len(rows),
        "source_count": len({row.source_key for row in rows}),
        "revenue": total_revenue,
        "profit": sum(known_profits),
        "unknown_profit": len(rows) - len(known_profits),
        "top_titles": title_rows[:limit],
    }


def analysis_imports(db: Session) -> list[AnalysisImport]:
    return list(db.scalars(select(AnalysisImport).order_by(AnalysisImport.imported_at.desc())))


def delete_analysis_import(db: Session, import_id: int) -> AnalysisImport:
    import_record = db.get(AnalysisImport, import_id)
    if import_record is None:
        raise ValueError("Импорт не найден.")
    analysis_item_ids = select(AnalysisItem.id).where(AnalysisItem.last_import_id == import_id)
    db.execute(delete(PriceObservation).where(PriceObservation.analysis_item_id.in_(analysis_item_ids)))
    db.execute(delete(AnalysisItem).where(AnalysisItem.last_import_id == import_id))
    db.delete(import_record)
    return import_record


def target_sync_users(settings: Settings) -> list[str]:
    my_id = (settings.my_id or "").strip()
    if not my_id:
        raise ValueError("Не задан MY_ID.")
    users = [user.strip() for user in settings.total_users if user.strip()]
    if not users:
        raise ValueError("Не задан TOTAL_USERS.")
    return [user for user in users if user != my_id]


def sync_code(user_id: str) -> str:
    return f"sync_to_{user_id}"


def croc_subprocess_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        "startupinfo": startupinfo,
    }


def send_current_export_via_croc(db: Session, settings: Settings, *, timeout_seconds: int = 90) -> CrocSendResult | None:
    path = CURRENT_EXPORT_PATH
    if not path.exists():
        return None
    payload = parse_analysis_export(path.read_bytes())
    targets: list[CrocTargetResult] = []
    for user_id in target_sync_users(settings):
        code = sync_code(user_id)
        command = [settings.croc_path, "send", "--code", code, str(path)]
        try:
            result = subprocess.run(
                command,
                cwd=path.parent,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                **croc_subprocess_kwargs(),
            )
        except FileNotFoundError:
            targets.append(CrocTargetResult(user_id=user_id, code=code, return_code=None, success=False, error="croc не найден"))
            break
        except subprocess.TimeoutExpired:
            targets.append(CrocTargetResult(user_id=user_id, code=code, return_code=None, success=False, error="таймаут ожидания croc"))
            continue
        targets.append(
            CrocTargetResult(
                user_id=user_id,
                code=code,
                return_code=result.returncode,
                success=result.returncode == 0,
                error=(result.stderr or result.stdout or "").strip()[:500] or None,
            )
        )

    synced_item_ids: list[int] = []
    if any(target.success for target in targets):
        synced_item_ids = mark_export_payload_synced(db, payload)
        path.unlink(missing_ok=True)
    return CrocSendResult(path=path, targets=targets, synced_item_ids=synced_item_ids)


def mark_export_payload_synced(db: Session, payload: dict[str, Any]) -> list[int]:
    item_ids = exported_item_ids(payload)
    if item_ids:
        db.execute(update(Item).where(Item.id.in_(item_ids)).values(is_synced=True))
    return item_ids


def exported_item_ids(payload: dict[str, Any]) -> list[int]:
    sync = payload.get("sync") if isinstance(payload.get("sync"), dict) else {}
    raw_ids = sync.get("item_ids") if isinstance(sync.get("item_ids"), list) else []
    result: list[int] = []
    for value in raw_ids:
        try:
            result.append(int(value))
        except (TypeError, ValueError):
            continue
    if result:
        return result
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    for raw_item in items:
        if not isinstance(raw_item, dict):
            continue
        try:
            result.append(int(raw_item.get("source_item_id")))
        except (TypeError, ValueError):
            continue
    return result


def import_received_json_files(db: Session, inbox: Path = QUEUE_IMPORTS_DIR) -> list[ImportResult]:
    inbox.mkdir(parents=True, exist_ok=True)
    results: list[ImportResult] = []
    for path in sorted(inbox.glob("*.json"), key=lambda item: item.stat().st_mtime):
        result = import_analysis_export(db, path.read_bytes(), filename=path.name)
        results.append(result)
        path.unlink(missing_ok=True)
    return results


def receive_once_via_croc(settings: Settings, *, timeout_seconds: int | None = None) -> list[Path]:
    my_id = (settings.my_id or "").strip()
    if not my_id:
        raise ValueError("Не задан MY_ID.")
    QUEUE_IMPORTS_DIR.mkdir(parents=True, exist_ok=True)
    started_at = time.time()
    command = [settings.croc_path, "--yes", "--remember", sync_code(my_id)]
    subprocess.run(command, cwd=QUEUE_IMPORTS_DIR, check=True, timeout=timeout_seconds, **croc_subprocess_kwargs())
    return [
        path
        for path in sorted(QUEUE_IMPORTS_DIR.glob("*.json"), key=lambda item: item.stat().st_mtime)
        if path.stat().st_mtime >= started_at
    ]


def safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def datetime_to_json(value: datetime | None) -> str | None:
    return value.isoformat() if value else None

