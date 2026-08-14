"""FastAPI web application for the local inventory MVP."""

from __future__ import annotations

import json
import re
import math
from collections import Counter
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, joinedload

from app.config import ROOT_DIR, ensure_data_dirs, get_settings
from app.db import get_engine, init_db, make_session_factory
from app.analysis_exchange import (
    analysis_imports,
    analysis_sales_summary,
    build_download_export_payload,
    build_incremental_analysis_export_file,
    delete_analysis_import,
    import_analysis_export,
    mark_download_exported,
)
from app.avito_evaluator import NET_AFTER_SALE_RATE, estimate_sale_price_for_name, evaluate_avito_listing_payload
from app.inventory import (
    ACCESSORY_CATEGORY,
    ACCESSORY_TYPES,
    CONSOLE_CATEGORY,
    CONSOLE_MODELS,
    DISC_SURFACES,
    GAME_CATEGORY,
    GAME_COMPLETENESS,
    ITEM_STATUSES,
    LANGUAGES,
    ORIGINALITY,
    PLATFORMS,
    SOURCES,
    STORAGE_SIZES,
    TEST_RESULTS,
    ItemInput,
    add_subcategory,
    clean_optional,
    create_item,
    dashboard_stats,
    delete_category,
    ensure_alias,
    ensure_catalog_entry,
    export_items_csv,
    item_display_name,
    item_platform_or_model,
    item_profit,
    merge_catalog_entries,
    parse_date,
    seed_database,
    sell_item,
    set_item_available,
    to_float,
    to_int,
    top_level_category_name,
)
from app.market import MATCH_CONFIRMED, MATCH_EXCLUDED, confirm_market_match, import_avito_api_batch, market_estimate
from app.models import CatalogAlias, CatalogEntry, Category, GameDetail, Item, MarketImport, MarketListing, MarketListingMatch
from app.price_observations import (
    delete_market_listing_observation,
    price_observation_summary,
    price_observation_label,
    record_market_listing_observation,
    record_own_sale_observation,
    sync_price_observations,
)
from app.schemas import parse_datetime
from app.suggestion_sources import external_game_title_suggestions, normalize_lookup_text, profit_knowledge


ensure_data_dirs()
settings = get_settings()
engine = get_engine(settings=settings)
init_db(engine)
SessionLocal = make_session_factory(engine)
DISPLAY_TIMEZONE = ZoneInfo("Europe/Moscow")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    with SessionLocal() as session:
        seed_database(session)
        sync_price_observations(session)
        session.commit()
    yield


app = FastAPI(title="Учёт игр и приставок", lifespan=lifespan)
templates = Jinja2Templates(directory=str(ROOT_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(ROOT_DIR / "static")), name="static")


def api_json(payload: Any, status_code: int = 200) -> Response:
    return Response(
        content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        status_code=status_code,
        media_type="application/json; charset=utf-8",
    )


ITEM_TITLE_SUGGESTIONS: list[tuple[str, list[str], str]] = [
    ("PlayStation 4", ["PS4", "пс4", "плейстейшен 4", "play station 4"], "console"),
    ("PlayStation 5 Disc", ["PS5", "PS5 Disc", "пс5", "плейстейшен 5", "play station 5", "с дисководом"], "console"),
    ("PlayStation 5 Digital", ["PS5 Digital", "PS5 диджитал", "без дисковода", "digital edition"], "console"),
    ("PlayStation 5 Slim Disc", ["PS5 Slim", "PS5 Slim Disc", "пс5 слим", "слим с дисководом"], "console"),
    ("PlayStation 5 Slim Digital", ["PS5 Slim Digital", "PS5 Slim диджитал", "слим без дисковода"], "console"),
    ("DualShock 4", ["геймпад PS4", "джойстик PS4", "джостик PS4", "dual shock 4"], "accessory"),
    ("DualSense", ["геймпад PS5", "джойстик PS5", "джостик PS5", "dualsense PS5"], "accessory"),
    ("Кабель HDMI", ["HDMI-кабель", "hdmi", "шдмай", "кабель шдмай"], "accessory"),
    ("Кабель питания", ["сетевой кабель", "провод питания", "кабель питания PS4", "кабель питания PS5"], "accessory"),
    ("USB-кабель для геймпада", ["кабель зарядки", "провод зарядки", "micro usb", "usb-c"], "accessory"),
    ("Зарядная станция DualShock 4", ["зарядка для геймпадов PS4", "док-станция PS4"], "accessory"),
    ("Зарядная станция DualSense", ["зарядка для геймпадов PS5", "док-станция PS5"], "accessory"),
    ("Подставка", ["вертикальная подставка", "крепление приставки", "подставка PS4", "подставка PS5"], "accessory"),
    ("Гарнитура", ["наушники", "гарнитура PS4", "гарнитура PS5"], "accessory"),
    ("Камера", ["PS Camera", "камера PS4", "камера PS5"], "accessory"),
    ("Пульт", ["media remote", "пульт PS5", "пульт PS4"], "accessory"),
]


class AvitoMarketListingPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    external_id: str | None = None
    bot_listing_id: str | None = None
    title: str | None = None
    description: str | None = None
    url: str | None = None
    price: float | int | str | None = None
    currency: str | None = None
    address: str | None = None
    platform: str | None = None
    format: str | None = None
    type: str | None = None
    localization: str | None = None
    seller_name: str | None = None
    seller_user_key: str | None = None
    seller_type: str | None = None
    is_shop: bool | None = False
    posted_at: datetime | str | None = None
    scraped_at: datetime | str | None = None
    raw_json: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_dedup_key(self) -> "AvitoMarketListingPayload":
        fingerprint = ""
        if isinstance(self.raw_json, dict):
            fingerprint = str(self.raw_json.get("fingerprint") or "").strip()
        if not (str(self.external_id or "").strip() or str(self.url or "").strip() or fingerprint):
            raise ValueError("listing needs external_id, url or raw_json.fingerprint")
        return self


class AvitoMarketImportPayload(BaseModel):
    source: str | None = "adb_bot"
    filename: str = "adb_bot_live"
    scraped_at: datetime | None = None
    crm_sent_at: datetime | None = None
    listings: list[AvitoMarketListingPayload] = Field(default_factory=list)


class AvitoListingEvaluationPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    listing: dict[str, Any] = Field(default_factory=dict)
    extracted_items: list[dict[str, Any]] = Field(default_factory=list)


def get_db() -> Session:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def render(request: Request, template: str, context: dict[str, Any], status_code: int = 200) -> HTMLResponse:
    pricing_db = context.get("_pricing_db")
    base = {
        "request": request,
        "GAME_CATEGORY": GAME_CATEGORY,
        "CONSOLE_CATEGORY": CONSOLE_CATEGORY,
        "ACCESSORY_CATEGORY": ACCESSORY_CATEGORY,
        "item_statuses": ITEM_STATUSES,
        "sources": SOURCES,
        "platforms": PLATFORMS,
        "languages": LANGUAGES,
        "disc_surfaces": DISC_SURFACES,
        "test_results": TEST_RESULTS,
        "game_completeness": GAME_COMPLETENESS,
        "console_models": CONSOLE_MODELS,
        "storage_sizes": STORAGE_SIZES,
        "accessory_types": ACCESSORY_TYPES,
        "originality_values": ORIGINALITY,
        "item_display_name": item_display_name,
        "item_platform_or_model": item_platform_or_model,
        "item_profit": item_profit,
        "item_short_code": item_short_code,
        "disc_localization_value": disc_localization_value,
        "cost_label": cost_label,
        "money": money,
        "item_listing_price": lambda item: item_listing_price(item, pricing_db),
        "price_observation_label": price_observation_label,
        "datetime_label": datetime_label,
        "item_age_class": item_age_class,
        "item_age_title": item_age_title,
    }
    base.update(context)
    return templates.TemplateResponse(request, template, base, status_code=status_code)


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    created: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return render_inventory_page(request, db, scope="available", created=created)


@app.get("/bought", response_class=HTMLResponse)
def bought(
    request: Request,
    created: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return render_inventory_page(request, db, scope="available", created=created)


@app.get("/sold", response_class=HTMLResponse)
def sold(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return render_inventory_page(request, db, scope="sold")


@app.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    rows = inventory_items(db)
    return render(
        request,
        "stats.html",
        {
            "page_title": "Статистика",
            "stats": inventory_stats(rows, db),
        },
    )


@app.get("/api/stats/sales-chart")
def api_stats_sales_chart(period: str = "month", db: Session = Depends(get_db)) -> Response:
    sold_rows = [item for item in inventory_items(db) if item.status == "Продан"]
    return api_json(sales_chart_payload(sold_rows, period))


@app.get("/history", response_class=HTMLResponse)
def history_page(
    request: Request,
    imported: str | None = None,
    sent: str | None = None,
    deleted: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    rows = inventory_items(db)
    return render(
        request,
        "history.html",
        {
            "page_title": "История",
            "events": inventory_history_events(rows),
            "imports": analysis_imports(db),
            "analysis_summary": analysis_sales_summary(db),
            "market_api_summary": market_api_import_summary(db),
            "imported": imported,
            "sent": sent,
            "deleted": deleted,
            "error": error,
        },
    )


def market_api_import_summary(db: Session) -> dict[str, Any]:
    rows = list(db.scalars(select(MarketImport).order_by(MarketImport.imported_at.desc(), MarketImport.id.desc())))
    recent_listings = list(db.scalars(select(MarketListing).order_by(MarketListing.last_seen_at.desc()).limit(500)))
    listing_saved_times = [value for listing in recent_listings if (value := market_listing_bot_saved_at(listing))]
    crm_sent_times = [value for row in rows if (value := (row.crm_sent_at or row.scraped_at))]
    return {
        "batches_count": len(rows),
        "raw_count": sum(row.raw_count or 0 for row in rows),
        "unique_count": sum(row.unique_count or 0 for row in rows),
        "duplicate_count": sum(row.duplicate_count or 0 for row in rows),
        "last_import_at": rows[0].imported_at if rows else None,
        "last_crm_sent_at": max(crm_sent_times) if crm_sent_times else None,
        "last_listing_saved_at": max(listing_saved_times) if listing_saved_times else None,
        "recent": rows[:6],
        "recent_listings": [market_api_listing_card(listing) for listing in recent_listings[:24]],
    }


def market_listing_bot_saved_at(listing: MarketListing) -> datetime | None:
    raw_json = listing.raw_json if isinstance(listing.raw_json, dict) else {}
    raw_saved_at = parse_datetime(raw_json.get("bot_saved_at"))
    if raw_saved_at is not None:
        return raw_saved_at
    if raw_json.get("crm_sent_at") and listing.scraped_at:
        return listing.scraped_at
    return None


def market_listing_crm_sent_at(listing: MarketListing) -> datetime | None:
    raw_json = listing.raw_json if isinstance(listing.raw_json, dict) else {}
    return parse_datetime(raw_json.get("crm_sent_at")) or listing.last_seen_at


def market_api_listing_card(listing: MarketListing) -> dict[str, Any]:
    sent_at = market_listing_crm_sent_at(listing)
    title = short_market_listing_title(listing.title or listing.external_id or f"Объявление {listing.id}")
    return {
        "id": listing.id,
        "title": title,
        "sent_at": sent_at,
        "sent_at_label": datetime_label(sent_at),
    }


def short_market_listing_title(value: str, limit: int = 44) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1].rstrip()}…"


@app.get("/api/market/imports/avito/summary")
def api_market_import_summary(db: Session = Depends(get_db)):
    summary = market_api_import_summary(db)
    last_import_at = summary["last_import_at"]
    last_crm_sent_at = summary["last_crm_sent_at"]
    last_listing_saved_at = summary["last_listing_saved_at"]
    return api_json({
        "batches_count": summary["batches_count"],
        "raw_count": summary["raw_count"],
        "unique_count": summary["unique_count"],
        "duplicate_count": summary["duplicate_count"],
        "last_import_at": last_import_at.isoformat() if last_import_at else None,
        "last_import_label": datetime_label(last_import_at) if last_import_at else None,
        "last_crm_sent_at": last_crm_sent_at.isoformat() if last_crm_sent_at else None,
        "last_crm_sent_label": datetime_label(last_crm_sent_at) if last_crm_sent_at else None,
        "last_listing_saved_at": last_listing_saved_at.isoformat() if last_listing_saved_at else None,
        "last_listing_saved_label": datetime_label(last_listing_saved_at) if last_listing_saved_at else None,
        "recent_listings": [
            {
                "id": row["id"],
                "title": row["title"],
                "sent_at": row["sent_at"].isoformat() if row["sent_at"] else None,
                "sent_at_label": row["sent_at_label"],
            }
            for row in summary["recent_listings"]
        ],
        "recent": [
            {
                "id": row.id,
                "filename": row.filename,
                "raw_count": row.raw_count or 0,
                "unique_count": row.unique_count or 0,
                "duplicate_count": row.duplicate_count or 0,
                "scraped_at": row.scraped_at.isoformat() if row.scraped_at else None,
                "scraped_at_label": datetime_label(row.scraped_at) if row.scraped_at else None,
                "crm_sent_at": row.crm_sent_at.isoformat() if row.crm_sent_at else None,
                "crm_sent_at_label": datetime_label(row.crm_sent_at) if row.crm_sent_at else None,
                "imported_at": row.imported_at.isoformat() if row.imported_at else None,
                "imported_at_label": datetime_label(row.imported_at) if row.imported_at else None,
            }
            for row in summary["recent"]
        ],
    })


@app.get("/api/catalog/game-suggestions")
def api_catalog_game_suggestions(db: Session = Depends(get_db)):
    entries = all_catalog_entries(db)
    items: list[dict[str, Any]] = []
    alias_index: list[dict[str, Any]] = []
    for entry in entries:
        estimate = market_estimate(db, catalog_entry_id=entry.id)
        category_name = entry.category.name if entry.category else None
        item_type = "game"
        if category_name:
            lowered_category = category_name.casefold()
            if "console" in lowered_category or "пристав" in lowered_category or "консол" in lowered_category:
                item_type = "console"
            elif "access" in lowered_category or "аксесс" in lowered_category:
                item_type = "accessory"
        item_id = suggestion_key(entry.name).replace(" ", "_") or f"catalog_{entry.id}"
        aliases = [alias.name for alias in entry.aliases if alias.name]
        price = estimate.median_price or estimate.min_price or 0
        item = {
            "id": item_id,
            "catalog_entry_id": entry.id,
            "canonical_name": entry.name,
            "aliases": aliases,
            "platform": None,
            "item_type": item_type,
            "category": category_name,
            "confidence": estimate.confidence,
            "sort_weight": 10_000,
            "prices": {
                "shop_median_rub": price,
                "shop_min_rub": estimate.min_price,
                "shop_max_rub": estimate.max_price,
                "friend_resale_price_rub": price,
            },
            "price_observation_count": estimate.count or 0,
            "source": {"kind": "postgres_crm_catalog"},
        }
        items.append(item)
        for term in [entry.name, *aliases]:
            normalized = suggestion_key(term)
            if not normalized:
                continue
            alias_index.append(
                {
                    "item_id": item_id,
                    "catalog_entry_id": entry.id,
                    "canonical_name": entry.name,
                    "item_type": item_type,
                    "term": term,
                    "normalized": normalized,
                    "weight": 10_000,
                }
            )
    return api_json({"items": items, "alias_index": alias_index, "source": "postgres_crm"})


@app.get("/exchange", response_class=HTMLResponse)
def exchange_page(
    request: Request,
    imported: str | None = None,
    sent: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    params = {key: value for key, value in {"imported": imported, "sent": sent, "error": error}.items() if value}
    suffix = f"?{urlencode(params)}" if params else ""
    return RedirectResponse(f"/history{suffix}", status_code=303)


@app.get("/exchange/export")
@app.post("/exchange/export")
def exchange_export(db: Session = Depends(get_db)):
    try:
        result = build_download_export_payload(db, settings)
    except Exception as exc:
        query = urlencode({"error": f"Экспорт не сформирован: {exc}"})
        return RedirectResponse(f"/history?{query}", status_code=303)
    if result.exported_count == 0:
        query = urlencode({"sent": "Новых строк для синхронизации нет."})
        return RedirectResponse(f"/history?{query}", status_code=303)
    mark_download_exported(db, result)
    db.commit()
    exported_at = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%Y%m%d_%H%M%S")
    filename = f"crm_export_inventory_avito_{exported_at}.json"
    body = json.dumps(result.payload, ensure_ascii=False, indent=2).encode("utf-8")
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(content=body, media_type="application/json; charset=utf-8", headers=headers)


@app.post("/exchange/import")
async def exchange_import(file: UploadFile = File(...), db: Session = Depends(get_db)):
    try:
        data = await file.read()
        result = import_analysis_export(db, data, filename=file.filename)
        db.commit()
    except Exception as exc:
        db.rollback()
        query = urlencode({"error": f"Импорт не выполнен: {exc}"})
        return RedirectResponse(f"/history?{query}", status_code=303)
    if result.skipped:
        message = "Этот экспорт уже импортирован, дубли не добавлял."
    else:
        message = (
            f"Импортировано: товаров новых {result.created_count}, обновлено {result.updated_count}; "
            f"объявлений бота новых {result.market_created_count}, дублей {result.market_duplicate_count}."
        )
    return RedirectResponse(f"/history?{urlencode({'imported': message})}", status_code=303)


@app.post("/exchange/imports/{import_id}/delete")
def exchange_import_delete(import_id: int, db: Session = Depends(get_db)):
    try:
        import_record = delete_analysis_import(db, import_id)
        db.commit()
    except Exception as exc:
        db.rollback()
        query = urlencode({"error": f"Импорт не удалён: {exc}"})
        return RedirectResponse(f"/history?{query}", status_code=303)
    label = import_record.source_name or import_record.source_key
    query = urlencode({"deleted": f"Импорт удалён: {label}."})
    return RedirectResponse(f"/history?{query}", status_code=303)


@app.get("/bought/new-disc", response_class=HTMLResponse)
def new_disc(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return render_new_disc_form(request, db, errors=[], values={})


@app.post("/bought/new-disc")
async def create_disc(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    values = dict(form)
    title = clean_optional(form.get("disc_title")) or clean_optional(form.get("catalog_name"))
    if not title:
        return render_new_disc_form(
            request,
            db,
            errors=["Укажите название товара."],
            values=values,
            status_code=422,
        )

    default_game_category = game_category(db)
    category_id = to_int(form.get("category_id")) or default_game_category.id
    inferred_category_name = infer_item_category_name(title)
    if category_id == default_game_category.id and inferred_category_name and inferred_category_name != GAME_CATEGORY:
        category_id = system_category(db, inferred_category_name).id
    category = db.get(Category, category_id)
    if category is None:
        return render_new_disc_form(
            request,
            db,
            errors=["Выберите категорию товара."],
            values=values,
            status_code=422,
        )

    root_category = top_level_category_name(category)
    interface_language = subtitles_language = voice_language = None
    if root_category == GAME_CATEGORY or category.name == GAME_CATEGORY:
        localization = str(form.get("localization") or "full_ru")
        interface_language, subtitles_language, voice_language = localization_fields(localization)

    console_missing = form.getlist("missing_parts")
    console_detail_comment = None
    if str(form.get("console_complete") or "yes") == "no" and console_missing:
        console_detail_comment = "Не хватает: " + ", ".join(str(part) for part in console_missing if part)

    accessory_type = clean_optional(form.get("accessory_type"))
    if root_category == ACCESSORY_CATEGORY or category.name == ACCESSORY_CATEGORY:
        accessory_type = accessory_type or infer_accessory_type(title)

    cost = to_float(form.get("calculated_cost"))
    created_item = create_item(
        db,
        item_input=ItemInput(
            category_id=category.id,
            catalog_name=title,
            quantity=1,
            purchased_at=form.get("purchased_at"),
            source=form.get("source") or "Авито",
            source_url=form.get("source_url"),
            calculated_cost=cost,
            expected_sale_price=to_float(form.get("expected_sale_price")),
            comment=clean_optional(form.get("comment")),
            platform=clean_optional(form.get("platform")),
            interface_language=interface_language,
            subtitles_language=subtitles_language,
            voice_language=voice_language,
            disc_surface=clean_optional(form.get("disc_surface")),
            test_result=clean_optional(form.get("test_result")),
            completeness=item_completeness_from_form(form, root_category),
            completeness_comment=clean_optional(form.get("disc_edition")) or clean_optional(form.get("edition")),
            model=clean_optional(form.get("console_version"))
            or clean_optional(form.get("console_model"))
            or clean_optional(form.get("model")),
            storage_size=clean_optional(form.get("storage_size")),
            controllers_count=to_int(form.get("controllers_count")),
            accessory_type=accessory_type,
            originality=clean_optional(form.get("originality")) if accessory_type == "Геймпад" else None,
            condition=item_condition_from_form(form, root_category),
            detail_comment=console_detail_comment or clean_optional(form.get("detail_comment")),
        ),
    )
    db.commit()
    return RedirectResponse(f"/bought?created={created_item.code}", status_code=303)


@app.post("/items/{item_id}/delete")
def delete_item(item_id: int, db: Session = Depends(get_db)):
    item = db.get(Item, item_id)
    if item:
        db.delete(item)
        db.commit()
    return RedirectResponse("/bought", status_code=303)


@app.get("/items", response_class=HTMLResponse)
def items(
    request: Request,
    status: str | None = None,
    category_id: int | None = None,
    platform: str | None = None,
    q: str | None = None,
    db: Session = Depends(get_db),
):
    return RedirectResponse("/bought", status_code=303)


@app.get("/items/{item_id}/sell", response_class=HTMLResponse)
def sell_item_form(request: Request, item_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    item = load_item(db, item_id)
    if item is None:
        return render(request, "error.html", {"message": "Товар не найден"}, status_code=404)
    return render(request, "sell_item.html", {"item": item, "errors": []})


@app.post("/items/{item_id}/sell")
async def sell_item_post(request: Request, item_id: int, db: Session = Depends(get_db)):
    item = load_item(db, item_id)
    if item is None:
        return render(request, "error.html", {"message": "Товар не найден"}, status_code=404)
    form = await request.form()
    sale_price = to_float(form.get("sale_price"))
    if sale_price is None:
        return render(request, "sell_item.html", {"item": item, "errors": ["Укажите цену продажи."]}, status_code=422)
    item = sell_item(
        db,
        item_id=item_id,
        sale_price=sale_price,
        sold_at=parse_date(form.get("sold_at")),
        comment=clean_optional(form.get("comment")),
    )
    record_own_sale_observation(db, item)
    db.commit()
    return RedirectResponse("/sold", status_code=303)


@app.post("/items/{item_id}/available")
def available_item_post(item_id: int, db: Session = Depends(get_db)):
    item = set_item_available(db, item_id, clear_sale_fields=True)
    record_own_sale_observation(db, item)
    db.commit()
    return RedirectResponse("/bought", status_code=303)


@app.get("/items/{item_id}/edit", response_class=HTMLResponse)
def edit_item_form(request: Request, item_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    item = load_item(db, item_id)
    if item is None:
        return render(request, "error.html", {"message": "Товар не найден"}, status_code=404)
    return render(
        request,
        "item_edit.html",
        {
            "item": item,
            "suggestions": item_title_suggestions(db, game_category(db).id),
            "edition_suggestions": edition_suggestions(db),
            "errors": [],
        },
    )


@app.post("/items/{item_id}/edit")
async def edit_item(request: Request, item_id: int, db: Session = Depends(get_db)):
    item = load_item(db, item_id)
    if item is None:
        return render(request, "error.html", {"message": "Товар не найден"}, status_code=404)
    form = await request.form()
    title = clean_optional(form.get("disc_title")) or clean_optional(form.get("catalog_name"))
    if not title:
        return render(
            request,
            "item_edit.html",
            {
                "item": item,
                "suggestions": item_title_suggestions(db, game_category(db).id),
                "edition_suggestions": edition_suggestions(db),
                "errors": ["Укажите название товара."],
            },
            status_code=422,
        )

    item.calculated_cost = to_float(form.get("calculated_cost"))
    item.expected_sale_price = to_float(form.get("expected_sale_price"))
    item.purchased_at = parse_date(form.get("purchased_at"))
    item.source = clean_optional(form.get("source"))
    item.source_url = clean_optional(form.get("source_url"))
    item.comment = clean_optional(form.get("comment"))
    item.is_synced = False

    if item.game_detail is not None:
        category = game_category(db)
        item.category_id = category.id
        item.catalog_entry_id = ensure_catalog_entry(db, category_id=category.id, name=title).id
        interface_language, subtitles_language, voice_language = localization_fields(str(form.get("localization") or "full_ru"))
        item.game_detail.platform = clean_optional(form.get("platform"))
        item.game_detail.interface_language = interface_language
        item.game_detail.subtitles_language = subtitles_language
        item.game_detail.voice_language = voice_language
        item.game_detail.disc_surface = clean_optional(form.get("disc_surface"))
        item.game_detail.test_result = clean_optional(form.get("test_result"))
        item.game_detail.completeness = clean_optional(form.get("completeness"))
        item.game_detail.completeness_comment = clean_optional(form.get("disc_edition")) or clean_optional(form.get("edition"))
    elif item.console_detail is not None:
        category = system_category(db, CONSOLE_CATEGORY)
        item.category_id = category.id
        item.catalog_entry_id = ensure_catalog_entry(db, category_id=category.id, name=title).id
        item.console_detail.model = clean_optional(form.get("console_version")) or clean_optional(form.get("model"))
        item.console_detail.storage_size = clean_optional(form.get("storage_size"))
        item.console_detail.controllers_count = to_int(form.get("controllers_count"))
        item.console_detail.condition = clean_optional(form.get("console_condition"))
        item.console_detail.completeness = clean_optional(form.get("console_completeness"))
        item.console_detail.comment = clean_optional(form.get("detail_comment"))
    elif item.accessory_detail is not None:
        category = system_category(db, ACCESSORY_CATEGORY)
        item.category_id = category.id
        item.catalog_entry_id = ensure_catalog_entry(db, category_id=category.id, name=title).id
        accessory_type = clean_optional(form.get("accessory_type")) or infer_accessory_type(title)
        item.accessory_detail.accessory_type = accessory_type
        item.accessory_detail.originality = clean_optional(form.get("originality")) if accessory_type == "Геймпад" else None
        item.accessory_detail.condition = item_condition_from_form(form, ACCESSORY_CATEGORY)
        item.accessory_detail.comment = clean_optional(form.get("detail_comment"))

    db.commit()
    return RedirectResponse("/bought", status_code=303)


@app.get("/catalog", response_class=HTMLResponse)
def catalog(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    entries = list(
        db.scalars(
            select(CatalogEntry)
            .options(joinedload(CatalogEntry.aliases), joinedload(CatalogEntry.category), joinedload(CatalogEntry.items))
            .order_by(CatalogEntry.name)
        )
        .unique()
    )
    estimates = {entry.id: market_estimate(db, catalog_entry_id=entry.id) for entry in entries}
    return render(request, "catalog.html", {"entries": entries, "categories": all_categories(db), "estimates": estimates})


@app.post("/catalog/new")
async def catalog_new(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        entry = ensure_catalog_entry(
            db,
            category_id=int(form.get("category_id")),
            name=str(form.get("name") or ""),
        )
        for alias in (form.get("alias_1"), form.get("alias_2")):
            if alias:
                ensure_alias(db, entry, str(alias))
        db.commit()
    except ValueError as exc:
        db.rollback()
        return render(request, "error.html", {"message": str(exc)}, status_code=422)
    return RedirectResponse("/catalog", status_code=303)


@app.post("/catalog/{entry_id}/edit")
async def catalog_edit(entry_id: int, request: Request, db: Session = Depends(get_db)):
    entry = db.get(CatalogEntry, entry_id)
    if entry is None:
        return render(request, "error.html", {"message": "Позиция не найдена"}, status_code=404)
    form = await request.form()
    entry.name = str(form.get("name") or entry.name).strip()
    entry.category_id = int(form.get("category_id") or entry.category_id)
    db.query(CatalogAlias).filter(CatalogAlias.catalog_entry_id == entry.id).delete()
    for alias in (form.get("alias_1"), form.get("alias_2")):
        if alias:
            ensure_alias(db, entry, str(alias))
    db.commit()
    return RedirectResponse("/catalog", status_code=303)


@app.post("/catalog/merge")
async def catalog_merge(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        merge_catalog_entries(db, source_id=int(form.get("source_id")), target_id=int(form.get("target_id")))
        db.commit()
    except ValueError as exc:
        db.rollback()
        return render(request, "error.html", {"message": str(exc)}, status_code=422)
    return RedirectResponse("/catalog", status_code=303)


@app.get("/categories", response_class=HTMLResponse)
def categories(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return render(request, "categories.html", {"roots": category_tree(db), "categories": all_categories(db), "errors": []})


@app.post("/categories/add")
async def category_add(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        add_subcategory(db, parent_id=int(form.get("parent_id")), name=str(form.get("name") or ""))
        db.commit()
    except ValueError as exc:
        db.rollback()
        return render(request, "categories.html", {"roots": category_tree(db), "categories": all_categories(db), "errors": [str(exc)]}, status_code=422)
    return RedirectResponse("/categories", status_code=303)


@app.post("/categories/{category_id}/delete")
def category_delete(category_id: int, db: Session = Depends(get_db)):
    try:
        delete_category(db, category_id)
        db.commit()
    except ValueError:
        db.rollback()
    return RedirectResponse("/categories", status_code=303)


def require_market_import_access(authorization: str | None) -> None:
    token = (settings.market_import_token or "").strip()
    if not token:
        return
    expected = f"Bearer {token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Invalid market import token")


@app.post("/api/market/imports/avito")
def api_import_avito_market(
    payload: AvitoMarketImportPayload,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    require_market_import_access(authorization)
    try:
        result = import_avito_api_batch(
            db,
            source=payload.source,
            filename=payload.filename,
            scraped_at=payload.scraped_at,
            crm_sent_at=payload.crm_sent_at,
            listings=[listing.model_dump(mode="python") for listing in payload.listings],
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return api_json({
        "import_id": result.import_id,
        "raw_count": result.raw_count,
        "unique_count": result.unique_count,
        "duplicate_count": result.duplicate_count,
        "created_listing_ids": result.created_listing_ids,
        "duplicate_listing_ids": result.duplicate_listing_ids,
    })


@app.post("/api/market/evaluate-avito-listing")
def api_evaluate_avito_listing(
    payload: AvitoListingEvaluationPayload,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    require_market_import_access(authorization)
    body = payload.model_dump(mode="python")
    listing = payload.listing or {
        key: value for key, value in body.items() if key not in {"listing", "extracted_items"}
    }
    if not isinstance(listing, dict):
        raise HTTPException(status_code=422, detail="listing must be an object")
    return api_json(
        evaluate_avito_listing_payload(
            db,
            listing=listing,
            extracted_items=payload.extracted_items,
        )
    )


@app.post("/api/market/sightings/avito")
async def api_avito_market_sightings(
    request: Request,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    require_market_import_access(authorization)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="payload must be an object")
    sightings = payload.get("sightings")
    if not isinstance(sightings, list):
        raise HTTPException(status_code=422, detail="sightings must be a list")
    db.execute(
        text(
            """
            create table if not exists market_listing_sightings (
                id serial primary key,
                external_id varchar(80),
                url text,
                title text,
                price double precision,
                cycle_number integer,
                bootstrap_all boolean,
                catalog_position integer,
                source_url text,
                observed_at timestamp without time zone not null,
                raw_json json,
                created_at timestamp without time zone not null default now()
            )
            """
        )
    )
    inserted = 0
    updated_listings = 0
    observed_at = parse_datetime(payload.get("observed_at")) or datetime.now(timezone.utc).replace(tzinfo=None)
    for raw in sightings:
        if not isinstance(raw, dict):
            continue
        url = clean_optional(raw.get("url"))
        external_id = clean_optional(raw.get("external_id")) or avito_external_id_from_url(url)
        title = clean_optional(raw.get("title"))
        price = to_float(raw.get("price"))
        db.execute(
            text(
                """
                insert into market_listing_sightings
                (external_id, url, title, price, cycle_number, bootstrap_all, catalog_position, source_url, observed_at, raw_json)
                values (:external_id, :url, :title, :price, :cycle_number, :bootstrap_all, :catalog_position, :source_url, :observed_at, cast(:raw_json as json))
                """
            ),
            {
                "external_id": external_id,
                "url": url,
                "title": title,
                "price": price,
                "cycle_number": to_int(raw.get("cycle_number")),
                "bootstrap_all": bool(raw.get("bootstrap_all")),
                "catalog_position": to_int(raw.get("catalog_position")),
                "source_url": clean_optional(raw.get("source_url")),
                "observed_at": observed_at,
                "raw_json": json.dumps(raw, ensure_ascii=False),
            },
        )
        inserted += 1
        listing = None
        if external_id:
            listing = db.scalar(select(MarketListing).where(MarketListing.external_id == external_id))
        if listing is None and url:
            listing = db.scalar(select(MarketListing).where(MarketListing.url == url))
        if listing is not None:
            listing.last_seen_at = observed_at
            raw_json = listing.raw_json if isinstance(listing.raw_json, dict) else {}
            monitor_context = raw_json.get("monitor_context") if isinstance(raw_json.get("monitor_context"), dict) else {}
            monitor_context["last_seen_at"] = observed_at.isoformat()
            if monitor_context.get("first_seen_at") is None:
                monitor_context["first_seen_at"] = listing.first_seen_at.isoformat() if listing.first_seen_at else observed_at.isoformat()
            raw_json["monitor_context"] = monitor_context
            listing.raw_json = raw_json
            updated_listings += 1
    db.commit()
    return api_json({"ok": True, "inserted": inserted, "updated_listings": updated_listings})


def avito_external_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    match = re.search(r"_(\d{6,})(?:[/?#]|$)", url)
    if match:
        return match.group(1)
    match = re.search(r"/(\d{6,})(?:[/?#]|$)", url)
    return match.group(1) if match else None


@app.get("/market", response_class=HTMLResponse)
def market(request: Request, matched: str | None = None, db: Session = Depends(get_db)) -> HTMLResponse:
    stmt = (
        select(MarketListing)
        .options(joinedload(MarketListing.matches).joinedload(MarketListingMatch.catalog_entry))
        .order_by(MarketListing.id.desc())
        .limit(300)
    )
    listings = list(db.scalars(stmt).unique())
    if matched == "yes":
        listings = [listing for listing in listings if listing.matches and listing.matches[-1].catalog_entry_id]
    if matched == "no":
        listings = [listing for listing in listings if not listing.matches or not listing.matches[-1].catalog_entry_id]
    return render(
        request,
        "market.html",
        {"listings": listings, "catalog_entries": all_catalog_entries(db), "matched": matched},
    )


@app.post("/market/{listing_id}/match")
async def market_match(listing_id: int, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    entry_id = to_int(form.get("catalog_entry_id"))
    status = str(form.get("status") or MATCH_CONFIRMED)
    confirm_market_match(db, listing_id=listing_id, catalog_entry_id=entry_id, status=status)
    listing = db.get(MarketListing, listing_id)
    if listing is not None:
        record_market_listing_observation(db, listing)
    db.commit()
    return RedirectResponse("/market", status_code=303)


@app.post("/market/{listing_id}/exclude")
def market_exclude(listing_id: int, db: Session = Depends(get_db)):
    confirm_market_match(db, listing_id=listing_id, catalog_entry_id=None, status=MATCH_EXCLUDED)
    delete_market_listing_observation(db, listing_id)
    db.commit()
    return RedirectResponse("/market", status_code=303)


@app.get("/export/items")
def export_items(db: Session = Depends(get_db)):
    return FileResponse(export_items_csv(db), filename="items.csv", media_type="text/csv")


INVENTORY_FILTER_KEYS = (
    "q",
    "category_filter",
    "condition_filter",
    "completeness_filter",
    "cost_min",
    "cost_max",
)
SOLD_NEAR_ZERO_PROFIT = 100.0


def render_inventory_page(
    request: Request,
    db: Session,
    *,
    scope: str,
    created: str | None = None,
) -> HTMLResponse:
    all_rows = inventory_items(db)
    filters = inventory_filters_from_request(request)
    sold_rows = [item for item in all_rows if item.status == "Продан"]
    if scope == "sold":
        scoped_rows = sold_rows
        page_title = "Продано"
        empty_text = "Проданных товаров пока нет."
    else:
        scoped_rows = [item for item in all_rows if item.status != "Продан"]
        page_title = "Купил"
        empty_text = "Товаров пока нет. Добавьте первый товар."
    rows = apply_inventory_filters(scoped_rows, filters)
    stats = inventory_stats(all_rows, db)
    return render(
        request,
        "bought.html",
        {
            "items": rows,
            "stats": stats,
            "_pricing_db": db,
            "filters": filters,
            "filter_options": inventory_filter_options(scoped_rows),
            "search_suggestions": inventory_search_suggestions(scoped_rows),
            "page_title": page_title,
            "show_add_button": scope != "sold",
            "is_sold_page": scope == "sold",
            "empty_text": empty_text,
            "created": created,
            "created_id": item_short_code(created) if created else None,
        },
    )


def render_new_disc_form(
    request: Request,
    db: Session,
    *,
    errors: list[str],
    values: dict[str, Any],
    status_code: int = 200,
) -> HTMLResponse:
    category = game_category(db)
    console_category = system_category(db, CONSOLE_CATEGORY)
    accessory_category = system_category(db, ACCESSORY_CATEGORY)
    return render(
        request,
        "disc_form.html",
        {
            "errors": errors,
            "values": values,
            "category": category,
            "game_category": category,
            "console_category": console_category,
            "accessory_category": accessory_category,
            "suggestions": item_title_suggestions(db, category.id),
            "edition_suggestions": edition_suggestions(db),
            "today": utc_date(),
        },
        status_code=status_code,
    )


def bought_disc_items(
    db: Session,
    *,
    q: str | None,
    status: str | None,
    platform: str | None,
) -> list[Item]:
    rows = apply_inventory_filters(inventory_items(db), {"q": q or ""})
    if status:
        rows = [item for item in rows if item.status == status]
    if platform:
        rows = [item for item in rows if item_platform_or_model(item) == platform]
    return rows


def inventory_items(db: Session) -> list[Item]:
    rows = list(
        db.scalars(
            select(Item)
            .options(
                joinedload(Item.category),
                joinedload(Item.catalog_entry),
                joinedload(Item.game_detail),
                joinedload(Item.console_detail),
                joinedload(Item.accessory_detail),
            )
            .order_by(Item.id.desc())
        ).unique()
    )
    return rows


def inventory_filters_from_request(request: Request) -> dict[str, str]:
    return {key: str(request.query_params.get(key) or "").strip() for key in INVENTORY_FILTER_KEYS}


def inventory_history_events(items: list[Item], limit: int = 120) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for item in items:
        name = item_display_name(item)
        code = item_short_code(item)
        base = {
            "item_id": item.id,
            "code": code,
            "name": name,
            "category": item.category.name,
        }
        event_date = item.purchased_at or item.created_at
        if event_date:
            events.append(
                {
                    **base,
                    "at": event_date,
                    "kind": "Закупка",
                    "tone": "event-purchase",
                    "title": f"Куплен товар {code}",
                    "text": f"{name}. Себестоимость: {cost_label(item.calculated_cost)}.",
                }
            )
        if item.updated_at and item.created_at and abs((item.updated_at - item.created_at).total_seconds()) > 1:
            events.append(
                {
                    **base,
                    "at": item.updated_at,
                    "kind": "Изменение",
                    "tone": "event-update",
                    "title": f"Обновлена карточка {code}",
                    "text": "",
                }
            )
        if item.status == "Продан":
            sale_time = item.sold_at or item.updated_at or item.created_at
            events.append(
                {
                    **base,
                    "at": sale_time,
                    "kind": "Продажа",
                    "tone": "event-sale",
                    "title": f"Продан товар {code}",
                    "text": f"{name}. Цена: {money(item.sale_price)}. Прибыль: {money(item_profit(item))}.",
                }
            )
    events.sort(key=lambda event: event["at"] or datetime.min, reverse=True)
    for event in events:
        event["icon"] = history_event_icon(event["kind"], event["tone"])
    return events[:limit]


def history_event_icon(kind: str, tone: str) -> str:
    icons = {
        "Закупка": "📦",
        "Добавление": "📦",
        "Продажа": "🤝",
        "Изменение": "✏️",
        "Удаление": "🗑️",
        "Возврат": "🔄",
    }
    return icons.get(kind) or {
        "event-purchase": "📦",
        "event-create": "📦",
        "event-sale": "🤝",
        "event-update": "✏️",
        "event-delete": "🗑️",
        "event-return": "🔄",
    }.get(tone, "•")


def inventory_stats(items: list[Item], db: Session | None = None) -> dict[str, Any]:
    sold_rows = [item for item in items if item.status == "Продан"]
    available_rows = [item for item in items if item.status == "Есть"]
    stock_cost = sum(item.calculated_cost or 0 for item in available_rows)
    sold_stats = sold_inventory_stats(sold_rows)
    return {
        "total": len(items),
        "available": len(available_rows),
        "sold": len(sold_rows),
        "stock_cost": stock_cost,
        "category_rows": inventory_category_rows(items),
        "sold_count_by_category": sold_count_by_category(sold_rows),
        "average_profit_by_category": average_profit_by_category(sold_rows),
        "sales_chart": sales_chart_payload(sold_rows, "month"),
        "business_metrics": business_metrics(sold_rows, sold_stats),
        "sale_speed_by_category": sale_speed_by_category(sold_rows),
        **sold_stats,
    }


def business_metrics(sold_rows: list[Item], sold_stats: dict[str, Any]) -> list[dict[str, Any]]:
    sold_cost = sold_stats["sold_cost"]
    sold_profit = sold_stats["sold_profit"]
    sold_roi = sold_profit / sold_cost * 100 if sold_cost else None
    speed_days = sale_speed_days(sold_rows)
    return [
        {
            "label": "ROI продаж",
            "value": percent_label(sold_roi),
            "caption": "прибыль / себестоимость проданного",
            "class": percent_tone(sold_roi, good_at=30, neutral_at=0),
        },
        {
            "label": "Скорость продажи",
            "value": format_day_count(speed_days) if speed_days is not None else "—",
            "caption": "медиана: дата продажи − дата покупки" if speed_days is not None else "Недостаточно данных",
            "class": "metric-neutral",
            "expandable": True,
        },
    ]


def sold_count_by_category(sold_rows: list[Item]) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    for item in sold_rows:
        counter[top_level_category_name(item.category)] += 1
    return [
        {"name": name, "count": count}
        for name, count in sorted(counter.items(), key=lambda pair: (-pair[1], pair[0].casefold()))
    ]


def average_profit_by_category(sold_rows: list[Item]) -> list[dict[str, Any]]:
    groups: dict[str, list[float]] = {}
    for item in sold_rows:
        profit = item_profit(item)
        if profit is None:
            continue
        groups.setdefault(top_level_category_name(item.category), []).append(profit)
    return [
        {"name": name, "average_profit": sum(values) / len(values)}
        for name, values in sorted(groups.items(), key=lambda pair: pair[0].casefold())
        if values
    ]


def sale_speed_days(sold_rows: list[Item]) -> int | None:
    values = [
        max((item.sold_at.date() - item.purchased_at.date()).days, 0)
        for item in sold_rows
        if item.sold_at is not None and item.purchased_at is not None
    ]
    return int(round(median(values))) if values else None


def sale_speed_by_category(sold_rows: list[Item]) -> list[dict[str, Any]]:
    groups: dict[str, list[int]] = {}
    for item in sold_rows:
        if item.sold_at is None or item.purchased_at is None:
            continue
        groups.setdefault(top_level_category_name(item.category), []).append(
            max((item.sold_at.date() - item.purchased_at.date()).days, 0)
        )
    return [
        {"name": name, "days": int(round(median(values))), "count": len(values)}
        for name, values in sorted(groups.items(), key=lambda pair: pair[0].casefold())
        if values
    ]


def capital_category_rows(
    available_items: list[Item],
    sold_items: list[Item],
    valuation_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    valuation_by_id = {row["item"].id: row for row in valuation_rows}
    sold_by_category = Counter(item.category.name for item in sold_items)
    total_stock_cost = sum(item.calculated_cost or 0 for item in available_items) or 1
    rows: dict[str, dict[str, Any]] = {}
    for item in available_items:
        row = rows.setdefault(
            item.category.name,
            {
                "name": item.category.name,
                "available": 0,
                "sold": sold_by_category.get(item.category.name, 0),
                "stock_cost": 0.0,
                "matched_count": 0,
                "estimated_net": 0.0,
                "estimated_cost": 0.0,
                "potential_profit": 0.0,
                "age_sum": 0,
                "age_count": 0,
            },
        )
        row["available"] += 1
        row["stock_cost"] += item.calculated_cost or 0
        age_days = item_age_days(item)
        if age_days is not None:
            row["age_sum"] += age_days
            row["age_count"] += 1
        valuation = valuation_by_id.get(item.id)
        if valuation is not None:
            row["matched_count"] += 1
            row["estimated_net"] += valuation["net_after_sale"]
            row["estimated_cost"] += valuation["cost"]
            row["potential_profit"] += valuation["profit"]

    result = sorted(rows.values(), key=lambda row: (-row["stock_cost"], row["name"].casefold()))
    for row in result:
        roi = row["potential_profit"] / row["estimated_cost"] * 100 if row["estimated_cost"] else None
        avg_age = round(row["age_sum"] / row["age_count"]) if row["age_count"] else None
        row["roi_percent"] = roi
        row["roi_label"] = percent_label(roi)
        row["roi_class"] = percent_tone(roi, good_at=30, neutral_at=0)
        row["profit_class"] = money_tone(row["potential_profit"])
        row["coverage_label"] = f"{row['matched_count']} / {row['available']}"
        row["average_age_days"] = avg_age
        row["average_age_label"] = format_day_count(avg_age) if avg_age is not None else "нет даты"
        row["stock_share"] = round(row["stock_cost"] / total_stock_cost * 100, 1)
    return result


def pricing_recommendation_rows(
    available_items: list[Item],
    valuation_rows: list[dict[str, Any]],
    *,
    limit: int = 16,
) -> list[dict[str, Any]]:
    valuation_by_id = {row["item"].id: row for row in valuation_rows}
    rows = [pricing_recommendation_row(item, valuation_by_id.get(item.id)) for item in available_items]
    rows.sort(key=lambda row: (-row["rank"], -(row["age_days"] or 0), -(row["cost"] or 0), row["name"].casefold()))
    return rows[:limit]


def pricing_recommendation_row(item: Item, valuation: dict[str, Any] | None) -> dict[str, Any]:
    cost = item.calculated_cost
    minimum_price = minimum_sale_price(cost)
    market_price = valuation_market_price(valuation)
    auto_price = auto_sale_price(market_price, minimum_price)
    expected_net = valuation["net_after_sale"] if valuation is not None else None
    profit = valuation["profit"] if valuation is not None else None
    age_days = item_age_days(item)
    advice, rank = pricing_advice(item, valuation, minimum_price, auto_price, profit, age_days)
    return {
        "item": item,
        "name": item_display_name(item),
        "cost": cost,
        "minimum_price": minimum_price,
        "auto_price": auto_price,
        "expected_net": expected_net,
        "profit": profit,
        "profit_class": money_tone(profit),
        "advice": advice,
        "rank": rank,
        "age_days": age_days,
        "age_label": format_day_count(age_days) if age_days is not None else "нет даты",
        "source": valuation["price_source"] if valuation is not None else "нет оценки",
    }


def sell_priority_rows(
    available_items: list[Item],
    sold_items: list[Item],
    valuation_rows: list[dict[str, Any]],
    *,
    limit: int = 12,
) -> list[dict[str, Any]]:
    valuation_by_id = {row["item"].id: row for row in valuation_rows}
    sold_by_title = Counter(normalize_lookup_text(item_display_name(item)) for item in sold_items)
    sold_by_category = Counter(item.category.name for item in sold_items)
    rows: list[dict[str, Any]] = []
    for item in available_items:
        valuation = valuation_by_id.get(item.id)
        pricing = pricing_recommendation_row(item, valuation)
        age_days = pricing["age_days"] or 0
        cost = item.calculated_cost or 0
        profit = pricing["profit"]
        sold_title_count = sold_by_title.get(normalize_lookup_text(item_display_name(item)), 0)
        sold_category_count = sold_by_category.get(item.category.name, 0)
        score = min(age_days, 90) / 90 * 35
        score += min(cost, 15_000) / 15_000 * 20
        if profit is None:
            score += 4
        else:
            cost_base = max(cost, 1)
            score += clamp(profit / cost_base * 30, -15, 30)
        score += min(sold_title_count * 8, 24)
        score += min(sold_category_count * 2, 12)
        if age_days >= 45:
            score += 10
        if age_days >= 90:
            score += 8
        rows.append(
            {
                **pricing,
                "score": round(score),
                "priority_class": priority_tone(score),
                "priority_label": priority_label(score),
                "reason": priority_reason(item, pricing, sold_title_count, sold_category_count),
            }
        )
    rows.sort(key=lambda row: (-row["score"], -(row["age_days"] or 0), -(row["cost"] or 0), row["name"].casefold()))
    return rows[:limit]


def minimum_sale_price(cost: float | None) -> float | None:
    if cost is None:
        return None
    return round_price_up(max(0.0, cost) + minimum_target_profit(cost))


def minimum_target_profit(cost: float | None) -> float:
    value = max(0.0, cost or 0.0)
    if value == 0:
        return 300.0
    if value < 700:
        return 200.0
    if value < 2_500:
        return max(300.0, value * 0.20)
    if value < 10_000:
        return max(700.0, value * 0.16)
    return max(1_500.0, value * 0.12)


def valuation_market_price(valuation: dict[str, Any] | None) -> float | None:
    if valuation is None:
        return None
    for key in ("quick_sell_price", "base_market_price", "net_after_sale"):
        value = valuation.get(key)
        if value is not None and value > 0:
            return value
    return None


def auto_sale_price(market_price: float | None, minimum_price: float | None) -> float | None:
    if market_price is None:
        return None
    rounded = round_price_nearest(market_price)
    if minimum_price is not None and rounded < minimum_price:
        return round_price_up(minimum_price)
    return rounded


def item_sale_price_estimate(item: Item, db: Session | None = None) -> dict[str, Any] | None:
    if item.expected_sale_price is not None and item.expected_sale_price > 0:
        return {
            "expected_sell_price": float(item.expected_sale_price),
            "canonical_name": item_display_name(item),
            "item_type": item_price_item_type(item),
            "confidence": "high",
            "source": "manual",
            "source_label": "ручная цена",
            "reason": "ручная ожидаемая цена в карточке",
        }
    if db is None:
        return None
    estimate = estimate_sale_price_for_name(
        db,
        name=item_display_name(item),
        platform=item_platform_or_model(item),
        item_type=item_price_item_type(item),
        catalog_entry_id=item.catalog_entry_id,
    )
    if estimate is None:
        return None
    return {
        **estimate,
        "source_label": price_observation_label(estimate.get("source")),
    }


def item_price_item_type(item: Item) -> str:
    if item.game_detail:
        return "game"
    if item.console_detail:
        return "console"
    if item.accessory_detail:
        text = normalize_lookup_text(item_display_name(item))
        if any(term in text for term in ("dualshock", "dualsense", "controller", "джоистик", "джостик", "геймпад")):
            return "controller"
        return "accessory"
    return "unknown"


def pricing_advice(
    item: Item,
    valuation: dict[str, Any] | None,
    minimum_price: float | None,
    auto_price: float | None,
    profit: float | None,
    age_days: int | None,
) -> tuple[str, int]:
    if item.calculated_cost is None:
        return "Заполнить себестоимость", 95
    if valuation is None:
        return "Проверить рынок вручную", 70
    if profit is not None and profit < -SOLD_NEAR_ZERO_PROFIT:
        return "Не снижать: база даёт минус", 90
    if age_days is not None and age_days >= 45 and profit is not None and profit > minimum_target_profit(item.calculated_cost):
        return "Давно лежит, можно делать скидку", 88
    if auto_price is not None and minimum_price is not None and auto_price <= minimum_price:
        return "Не опускаться ниже минимума", 78
    if profit is not None and profit > minimum_target_profit(item.calculated_cost):
        return "Хороший кандидат на продажу", 72
    if age_days is not None and age_days >= 30:
        return "Лежит долго, проверить цену", 66
    return "Цена выглядит нормально", 40


def priority_reason(
    item: Item,
    pricing: dict[str, Any],
    sold_title_count: int,
    sold_category_count: int,
) -> str:
    age_days = pricing["age_days"]
    profit = pricing["profit"]
    cost = item.calculated_cost or 0
    if age_days is not None and age_days >= 45 and profit is not None and profit > SOLD_NEAR_ZERO_PROFIT:
        return "долго лежит, запас на скидку есть"
    if cost >= 7_000 and age_days is not None and age_days >= 14:
        return "много денег заморожено"
    if sold_title_count:
        return f"эта позиция уже продавалась: {sold_title_count} шт"
    if sold_category_count >= 3:
        return f"категория продаётся: {sold_category_count} шт"
    if profit is None:
        return "нет оценки, нужна ручная проверка"
    if profit > minimum_target_profit(item.calculated_cost):
        return "по базе хороший плюс"
    if profit < -SOLD_NEAR_ZERO_PROFIT:
        return "ожидаемый минус, цену лучше не ронять"
    return "обычный контроль цены"


def priority_tone(score: float) -> str:
    if score >= 70:
        return "metric-bad"
    if score >= 45:
        return "metric-neutral"
    return "metric-good"


def priority_label(score: float) -> str:
    if score >= 70:
        return "сначала"
    if score >= 45:
        return "следить"
    return "нормально"


def money_tone(value: float | None) -> str:
    if value is None:
        return "metric-neutral"
    if value > SOLD_NEAR_ZERO_PROFIT:
        return "metric-good"
    if value < -SOLD_NEAR_ZERO_PROFIT:
        return "metric-bad"
    return "metric-neutral"


def round_price_nearest(value: float, step: int = 50) -> float:
    return float(max(0, round(value / step) * step))


def round_price_up(value: float, step: int = 50) -> float:
    return float(max(0, math.ceil(value / step) * step))


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def percent_label(value: float | None) -> str:
    if value is None:
        return "нет данных"
    return f"{value:.1f}%"


def percent_tone(value: float | None, *, good_at: float, neutral_at: float) -> str:
    if value is None:
        return "metric-neutral"
    if value >= good_at:
        return "metric-good"
    if value >= neutral_at:
        return "metric-neutral"
    return "metric-bad"


def inverse_percent_tone(value: float | None, *, good_at: float, neutral_at: float) -> str:
    if value is None:
        return "metric-neutral"
    if value <= good_at:
        return "metric-good"
    if value <= neutral_at:
        return "metric-neutral"
    return "metric-bad"


def rough_stock_valuation_rows(items: list[Item], db: Session | None = None) -> list[dict[str, Any]]:
    return [row for item in items if (row := rough_stock_item_valuation(item, db)) is not None]


def rough_stock_valuation_stats(items: list[Item], rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    rows = rows if rows is not None else rough_stock_valuation_rows(items)
    estimated_net_revenue = sum(row["net_after_sale"] for row in rows)
    estimated_profit = sum(row["profit"] for row in rows)
    estimated_cost = sum(row["cost"] for row in rows)
    known_profits = [row["profit"] for row in rows]
    return {
        "total_count": len(items),
        "matched_count": len(rows),
        "unknown_count": len(items) - len(rows),
        "estimated_net_revenue": estimated_net_revenue,
        "estimated_profit": estimated_profit,
        "estimated_cost": estimated_cost,
        "roi_percent": estimated_profit / estimated_cost * 100 if estimated_cost else 0,
        "positive_count": sum(1 for value in known_profits if value > SOLD_NEAR_ZERO_PROFIT),
        "near_zero_count": sum(1 for value in known_profits if abs(value) <= SOLD_NEAR_ZERO_PROFIT),
        "negative_count": sum(1 for value in known_profits if value < -SOLD_NEAR_ZERO_PROFIT),
        "rows": sorted(rows, key=lambda row: (row["profit"], row["net_after_sale"]), reverse=True)[:14],
    }


def rough_stock_item_valuation(item: Item, db: Session | None = None) -> dict[str, Any] | None:
    if item.calculated_cost is None:
        return None
    estimate = item_sale_price_estimate(item, db)
    if estimate is not None:
        expected_sell_price = estimate["expected_sell_price"]
        net_after_sale = expected_sell_price * NET_AFTER_SALE_RATE
        cost = item.calculated_cost or 0
        profit = net_after_sale - cost
        return {
            "item": item,
            "match_name": estimate["canonical_name"],
            "item_type": estimate["item_type"],
            "price_source": estimate["source_label"],
            "price_confidence": estimate["confidence"],
            "net_after_sale": net_after_sale,
            "cost": cost,
            "profit": profit,
            "base_market_price": expected_sell_price,
            "quick_sell_price": expected_sell_price,
            "max_buy_price": None,
            "profit_class": (
                "metric-good"
                if profit > SOLD_NEAR_ZERO_PROFIT
                else "metric-bad"
                if profit < -SOLD_NEAR_ZERO_PROFIT
                else "metric-neutral"
            ),
        }
    match = match_profit_knowledge_item(item)
    if match is None:
        return None
    net_after_sale = item_net_after_sale_estimate(item, match)
    if net_after_sale is None:
        return None
    cost = item.calculated_cost or 0
    profit = net_after_sale - cost
    return {
        "item": item,
        "match_name": match.canonical_name,
        "item_type": match.item_type,
        "price_source": profit_price_source_label(match.price_source),
        "price_confidence": profit_confidence_label(match.price_confidence),
        "net_after_sale": net_after_sale,
        "cost": cost,
        "profit": profit,
        "base_market_price": match.base_market_price_rub,
        "quick_sell_price": match.quick_sell_price_rub,
        "max_buy_price": match.max_buy_price_rub,
        "profit_class": (
            "metric-good"
            if profit > SOLD_NEAR_ZERO_PROFIT
            else "metric-bad"
            if profit < -SOLD_NEAR_ZERO_PROFIT
            else "metric-neutral"
        ),
    }


def match_profit_knowledge_item(item: Item):
    knowledge = profit_knowledge()
    if not knowledge.items_by_id:
        return None
    allowed_types = profit_item_types(item)
    if not allowed_types:
        return None
    terms = [normalize_lookup_text(term) for term in item_profit_lookup_terms(item)]
    terms = [term for term in dict.fromkeys(terms) if term]
    if not terms:
        return None
    padded_text = " " + " ".join(terms) + " "
    best_score = -1
    best_item = None
    for alias, item_id, weight in knowledge.aliases:
        candidate = knowledge.items_by_id.get(item_id)
        if candidate is None or candidate.item_type not in allowed_types or not alias:
            continue
        score = 0
        if alias in terms:
            score = 100_000 + weight
        elif len(alias) >= 4 and f" {alias} " in padded_text:
            score = 50_000 + weight + len(alias)
        elif len(alias) >= 5 and any(alias in term or term in alias for term in terms if len(term) >= 5):
            score = 10_000 + weight + len(alias)
        if score > best_score:
            best_score = score
            best_item = candidate
    return best_item


def profit_item_types(item: Item) -> set[str]:
    if item.game_detail:
        return {"game"}
    if item.console_detail:
        return {"console"}
    if item.accessory_detail:
        text = normalize_lookup_text(" ".join(item_profit_lookup_terms(item)))
        if any(term in text for term in ("dualshock", "dualsense", "controller", "gejmpad", "джоистик", "джостик", "геймпад")):
            return {"controller"}
    return set()


def item_profit_lookup_terms(item: Item) -> list[str]:
    return [
        item_display_name(item),
        item_platform_or_model(item),
        item_detail_search_text(item),
    ]


def item_net_after_sale_estimate(item: Item, match: Any) -> float | None:
    if match.good_net_after_sale_rub is not None or match.bad_net_after_sale_rub is not None:
        if item_profit_condition(item) == "bad" and match.bad_net_after_sale_rub is not None:
            return match.bad_net_after_sale_rub
        return match.good_net_after_sale_rub or match.bad_net_after_sale_rub
    return match.net_after_sale_rub


def item_profit_condition(item: Item) -> str:
    condition = item_condition_text(item).casefold()
    if any(word in condition for word in ("не работает", "нераб", "сильно", "глубок", "бит", "слом")):
        return "bad"
    return "good"


def profit_price_source_label(value: str) -> str:
    labels = {
        "friend_resale_price": "дружеская цена",
        "shop_median": "медиана магазина",
        "manual_console_catalog": "ручная база",
        "manual_controller_value": "ручная база",
    }
    return labels.get(value, value or "база")


def profit_confidence_label(value: str) -> str:
    labels = {
        "high": "высокая",
        "medium": "средняя",
        "low": "низкая",
        "manual": "ручная",
    }
    return labels.get(value, value or "неизвестно")


def sold_inventory_stats(items: list[Item]) -> dict[str, Any]:
    known_profits = [profit for item in items if (profit := item_profit(item)) is not None]
    revenue = sum(item.sale_price or 0 for item in items)
    profit = sum(known_profits)
    sold_cost = sum(item.calculated_cost or 0 for item in items if item.calculated_cost is not None)
    result_segments = [
        {
            "label": "В плюс",
            "count": sum(1 for value in known_profits if value > SOLD_NEAR_ZERO_PROFIT),
            "class": "metric-good",
            "color": "#3fb950",
        },
        {
            "label": "Почти в ноль",
            "count": sum(1 for value in known_profits if abs(value) <= SOLD_NEAR_ZERO_PROFIT),
            "class": "metric-neutral",
            "color": "#ffd166",
        },
        {
            "label": "В минус",
            "count": sum(1 for value in known_profits if value < -SOLD_NEAR_ZERO_PROFIT),
            "class": "metric-bad",
            "color": "#ff6b6b",
        },
    ]
    return {
        "sold_revenue": revenue,
        "sold_profit": profit,
        "sold_cost": sold_cost,
        "sold_average_price": revenue / len(items) if items else 0,
        "sold_margin_percent": (profit / revenue * 100) if revenue else 0,
        "sold_unknown_profit": len(items) - len(known_profits),
        "sold_positive": result_segments[0]["count"],
        "sold_near_zero": result_segments[1]["count"],
        "sold_negative": result_segments[2]["count"],
        "result_segments": result_segments,
        "result_donut_style": donut_style(result_segments),
        "sold_profit_rows": actual_sold_profit_rows(items),
    }


def actual_sold_profit_rows(items: list[Item]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    ordered = sorted(items, key=lambda item: item.sold_at or item.updated_at or item.created_at or datetime.min, reverse=True)
    for item in ordered:
        profit = item_profit(item)
        rows.append(
            {
                "item": item,
                "sold_at": item.sold_at or item.updated_at or item.created_at,
                "cost": item.calculated_cost,
                "sale_price": item.sale_price,
                "profit": profit,
                "profit_class": (
                    "metric-good"
                    if profit is not None and profit > SOLD_NEAR_ZERO_PROFIT
                    else "metric-bad"
                    if profit is not None and profit < -SOLD_NEAR_ZERO_PROFIT
                    else "metric-neutral"
                ),
            }
        )
    return rows


def inventory_category_rows(items: list[Item]) -> list[dict[str, Any]]:
    total = len(items) or 1
    rows: dict[str, dict[str, Any]] = {}
    for item in items:
        category_name = top_level_category_name(item.category)
        row = rows.setdefault(
            category_name,
            {"name": category_name, "total": 0, "available": 0, "sold": 0, "stock_cost": 0.0},
        )
        row["total"] += 1
        if item.status == "Продан":
            row["sold"] += 1
        else:
            row["available"] += 1
            row["stock_cost"] += item.calculated_cost or 0
    result = sorted(rows.values(), key=lambda row: (-row["total"], row["name"].casefold()))
    for row in result:
        row["share"] = round(row["total"] / total * 100, 1)
    return result


def daily_sales_rows(items: list[Item], days: int = 14) -> list[dict[str, Any]]:
    today = date.today()
    dates = [today - timedelta(days=days - 1 - index) for index in range(days)]
    rows = {
        day: {"date": day, "label": day.strftime("%d.%m"), "count": 0, "revenue": 0.0, "profit": 0.0}
        for day in dates
    }
    for item in items:
        sold_date = item.sold_at or item.updated_at or item.created_at
        if sold_date is None:
            continue
        day = sold_date.date()
        if day not in rows:
            continue
        rows[day]["count"] += 1
        rows[day]["revenue"] += item.sale_price or 0
        profit = item_profit(item)
        rows[day]["profit"] += profit or 0
    max_revenue = max((row["revenue"] for row in rows.values()), default=0) or 1
    for row in rows.values():
        row["revenue_height"] = round(row["revenue"] / max_revenue * 100, 1) if row["revenue"] else 0
        profit = row["profit"]
        row["profit_class"] = (
            "metric-good"
            if profit > SOLD_NEAR_ZERO_PROFIT
            else "metric-bad"
            if profit < -SOLD_NEAR_ZERO_PROFIT
            else "metric-neutral"
        )
    return list(rows.values())


SALES_CHART_PERIODS = {
    "month": "Месяц",
    "weeks": "Недели",
    "year": "Год",
}

RU_MONTH_NAMES = [
    "январь",
    "февраль",
    "март",
    "апрель",
    "май",
    "июнь",
    "июль",
    "август",
    "сентябрь",
    "октябрь",
    "ноябрь",
    "декабрь",
]

RU_MONTH_SHORT = [
    "янв",
    "фев",
    "мар",
    "апр",
    "май",
    "июн",
    "июл",
    "авг",
    "сен",
    "окт",
    "ноя",
    "дек",
]


def sales_chart_payload(items: list[Item], period: str = "month") -> dict[str, Any]:
    period = period if period in SALES_CHART_PERIODS else "month"
    rows = empty_sales_chart_rows(period)
    for item in items:
        sold_date = item.sold_at or item.updated_at or item.created_at
        if sold_date is None:
            continue
        key = sales_chart_bucket_key(sold_date.date(), period)
        if key is None or key not in rows:
            continue
        rows[key]["sold_count"] += 1
        rows[key]["count"] += 1
        rows[key]["revenue"] += item.sale_price or 0
        profit = item_profit(item)
        if profit is not None:
            rows[key]["profit"] += profit
    prepared_rows = prepare_sales_chart_rows(list(rows.values()))
    return {
        "period": period,
        "period_label": SALES_CHART_PERIODS[period],
        "range_label": sales_chart_range_label(period),
        "rows": prepared_rows,
    }


def empty_sales_chart_rows(period: str) -> dict[Any, dict[str, Any]]:
    today = date.today()
    if period == "year":
        return {
            month: {
                "key": f"{today.year}-{month:02d}",
                "label": RU_MONTH_SHORT[month - 1],
                "full_label": f"{RU_MONTH_NAMES[month - 1]} {today.year}",
                "count": 0,
                "sold_count": 0,
                "revenue": 0.0,
                "profit": 0.0,
            }
            for month in range(1, 13)
        }
    if period == "weeks":
        starts = [today - timedelta(days=13 - index) for index in range(14)]
        return {
            day: {
                "key": day.isoformat(),
                "label": day.strftime("%d.%m"),
                "full_label": day.strftime("%d.%m.%Y"),
                "count": 0,
                "sold_count": 0,
                "revenue": 0.0,
                "profit": 0.0,
            }
            for day in starts
        }
    first_day = today.replace(day=1)
    if today.month == 12:
        next_month = date(today.year + 1, 1, 1)
    else:
        next_month = date(today.year, today.month + 1, 1)
    starts: list[date] = []
    cursor = first_day
    while cursor < next_month:
        starts.append(cursor)
        cursor += timedelta(days=7)
    return {
        start: {
            "key": start.isoformat(),
            "label": f"{start.strftime('%d.%m')}–{(min(start + timedelta(days=6), next_month - timedelta(days=1))).strftime('%d.%m')}",
            "full_label": f"{start.strftime('%d.%m.%Y')} — {(min(start + timedelta(days=6), next_month - timedelta(days=1))).strftime('%d.%m.%Y')}",
            "count": 0,
            "sold_count": 0,
            "revenue": 0.0,
            "profit": 0.0,
        }
        for start in starts
    }


def sales_chart_bucket_key(day: date, period: str) -> Any | None:
    today = date.today()
    if period == "year":
        if day.year != today.year:
            return None
        return day.month
    if period == "weeks":
        first_day = today - timedelta(days=13)
        if day < first_day or day > today:
            return None
        return day
    first_day = today.replace(day=1)
    if today.month == 12:
        next_month = date(today.year + 1, 1, 1)
    else:
        next_month = date(today.year, today.month + 1, 1)
    if day < first_day or day >= next_month:
        return None
    return first_day + timedelta(days=((day - first_day).days // 7) * 7)


def prepare_sales_chart_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    max_profit = max((abs(row["profit"]) for row in rows), default=0) or 1
    for row in rows:
        profit = row["profit"]
        row["profit_height"] = round(abs(profit) / max_profit * 100, 1) if profit else 0
        row["margin_percent"] = (profit / row["revenue"] * 100) if row["revenue"] else 0
        row["profit_class"] = (
            "metric-good"
            if profit > SOLD_NEAR_ZERO_PROFIT
            else "metric-bad"
            if profit < -SOLD_NEAR_ZERO_PROFIT
            else "metric-neutral"
        )
        row["revenue_label"] = money(row["revenue"])
        row["profit_label"] = money(row["profit"])
        row["margin_label"] = f"{row['margin_percent']:.1f}%"
    return rows


def sales_chart_range_label(period: str) -> str:
    today = date.today()
    if period == "year":
        return f"{today.year} год"
    if period == "weeks":
        start = today - timedelta(days=13)
        return f"{start.strftime('%d.%m.%Y')} — {today.strftime('%d.%m.%Y')}"
    first_day = today.replace(day=1)
    if today.month == 12:
        last_day = date(today.year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(today.year, today.month + 1, 1) - timedelta(days=1)
    return f"{first_day.strftime('%d.%m.%Y')} — {last_day.strftime('%d.%m.%Y')}"


def donut_style(segments: list[dict[str, Any]]) -> str:
    total = sum(segment["count"] for segment in segments)
    if total <= 0:
        return "conic-gradient(var(--line) 0 100%)"
    start = 0.0
    parts: list[str] = []
    for segment in segments:
        end = start + segment["count"] / total * 100
        if segment["count"]:
            parts.append(f"{segment['color']} {start:.2f}% {end:.2f}%")
        start = end
    return "conic-gradient(" + ", ".join(parts) + ")"


def apply_inventory_filters(rows: list[Item], filters: dict[str, str]) -> list[Item]:
    q = filters.get("q", "")
    if q:
        lowered = q.lower()
        rows = [
            item
            for item in rows
            if lowered in item_display_name(item).lower()
            or lowered in (item.code or "").lower()
            or lowered in item.category.name.lower()
            or lowered in item_platform_or_model(item).lower()
            or lowered in item_detail_search_text(item).lower()
        ]
    rows = filter_items_by_text(rows, filters.get("category_filter", ""), lambda item: item.category.name)
    rows = filter_items_by_text(rows, filters.get("condition_filter", ""), item_condition_text)
    rows = filter_items_by_text(rows, filters.get("completeness_filter", ""), item_completeness_text)
    cost_min = parse_price_filter(filters.get("cost_min"))
    cost_max = parse_price_filter(filters.get("cost_max"))
    if cost_min is not None:
        rows = [item for item in rows if (item.calculated_cost or 0) >= cost_min]
    if cost_max is not None:
        rows = [item for item in rows if (item.calculated_cost or 0) <= cost_max]
    return rows


def parse_price_filter(value: str | None) -> float | None:
    text = str(value or "").strip().replace(" ", "").replace("₽", "").replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def filter_items_by_text(rows: list[Item], value: str | None, extractor: Any) -> list[Item]:
    if not value:
        return rows
    lowered = value.strip().lower()
    if not lowered:
        return rows
    return [item for item in rows if lowered in str(extractor(item) or "").lower()]


def game_suggestions(db: Session, category_id: int) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    seen: set[str] = set()
    entries = sorted(
        [entry for entry in all_catalog_entries(db) if entry.category_id == category_id],
        key=lambda entry: (not entry.is_system, entry.name.casefold()),
    )
    for entry in entries:
        label = entry.name.strip()
        if not label or label.isdigit():
            continue
        terms = [label, *(alias.name.strip() for alias in entry.aliases if alias.name.strip())]
        term_keys = {suggestion_key(term) for term in terms if term}
        if not term_keys or term_keys & seen:
            seen.update(term_keys)
            continue
        seen.update(term_keys)
        suggestions.append({"label": label, "search": " ".join(terms).casefold(), "category": "game", "weight": 10_000})
    return sorted(suggestions, key=lambda item: (-int(item.get("weight") or 0), item["label"].casefold()))


def item_title_suggestions(db: Session, game_category_id: int) -> list[dict[str, Any]]:
    suggestions = game_suggestions(db, game_category_id)
    seen = {suggestion_key(suggestion["label"]) for suggestion in suggestions}
    for label, aliases, category in ITEM_TITLE_SUGGESTIONS:
        terms = [label, *aliases]
        term_keys = {suggestion_key(term) for term in terms if term.strip()}
        if not term_keys or term_keys & seen:
            seen.update(term_keys)
            continue
        seen.update(term_keys)
        suggestions.append({"label": label, "search": " ".join(terms).casefold(), "category": category, "weight": 9_500})
    for external in external_game_title_suggestions():
        key = suggestion_key(external.label)
        if not key or key in seen:
            continue
        seen.add(key)
        suggestions.append(
            {
                "label": external.label,
                "search": external.search,
                "category": external.category,
                "weight": external.weight,
            }
        )
    return sorted(suggestions, key=lambda item: (-int(item.get("weight") or 0), item["label"].casefold()))


def suggestion_key(value: str) -> str:
    text = value.casefold().replace("ё", "е")
    roman_map = {
        "viii": "8",
        "vii": "7",
        "vi": "6",
        "iv": "4",
        "iii": "3",
        "ii": "2",
        "ix": "9",
        "v": "5",
        "i": "1",
    }
    for roman, digit in roman_map.items():
        text = re.sub(rf"\b{roman}\b", digit, text)
    return re.sub(r"[^0-9a-zа-я]+", "", text)


def edition_suggestions(db: Session) -> list[dict[str, Any]]:
    default_values = [
        "Обычное",
        "Standard Edition",
        "Special Edition",
        "Limited Edition",
        "Collector's Edition",
        "Deluxe Edition",
        "Gold Edition",
        "Premium Edition",
        "Ultimate Edition",
        "Complete Edition",
        "Complete Collection",
        "Game of the Year Edition",
        "Definitive Edition",
        "Director's Cut",
        "Remastered",
        "Remake",
        "Royal Edition",
        "Legendary Edition",
        "Anniversary Edition",
        "Ultimate Evil Edition",
    ]
    values: dict[str, dict[str, Any]] = {
        value.casefold(): {"label": value, "search": value.casefold(), "is_default": True, "game_search": ""}
        for value in default_values
    }
    saved = db.execute(
        select(GameDetail.completeness_comment, CatalogEntry.name)
        .join(Item, GameDetail.item_id == Item.id)
        .join(CatalogEntry, Item.catalog_entry_id == CatalogEntry.id, isouter=True)
        .where(GameDetail.completeness_comment.is_not(None))
    )
    for value, game_name in saved:
        clean = value.strip()
        if not clean:
            continue
        key = clean.casefold()
        payload = values.setdefault(
            key,
            {"label": clean, "search": clean.casefold(), "is_default": False, "game_search": ""},
        )
        payload["is_default"] = bool(payload.get("is_default"))
        if game_name:
            game_terms = [str(payload.get("game_search") or ""), game_name, suggestion_key(game_name)]
            payload["game_search"] = " ".join(term for term in game_terms if term).casefold()
    return sorted(
        values.values(),
        key=lambda item: (not item.get("is_default"), str(item.get("label") or "").casefold()),
    )


def localization_fields(value: str) -> tuple[str, str, str]:
    if value == "ru_subs_ui":
        return "Русский", "Русский", "Английский"
    if value == "ru_ui":
        return "Русский", "Не указано", "Не указано"
    if value == "original":
        return "Английский", "Английский", "Английский"
    return "Русский", "Русский", "Русский"


def disc_localization_value(item: Item) -> str:
    detail = item.game_detail
    if detail is None:
        return "full_ru"
    values = (detail.interface_language, detail.subtitles_language, detail.voice_language)
    if values == ("Русский", "Русский", "Английский"):
        return "ru_subs_ui"
    if values == ("Русский", "Не указано", "Не указано"):
        return "ru_ui"
    if values == ("Английский", "Английский", "Английский"):
        return "original"
    return "full_ru"


def all_categories(db: Session) -> list[Category]:
    return list(db.scalars(select(Category).order_by(Category.parent_id, Category.name)))


def all_catalog_entries(db: Session) -> list[CatalogEntry]:
    return list(db.scalars(select(CatalogEntry).options(joinedload(CatalogEntry.aliases)).order_by(CatalogEntry.name)).unique())


def game_category(db: Session) -> Category:
    return system_category(db, GAME_CATEGORY)


def system_category(db: Session, name: str) -> Category:
    category = db.scalar(select(Category).where(Category.name == name))
    if category is None:
        raise ValueError(f"Не выполнен seed: категория {name} не найдена")
    return category


def item_completeness_from_form(form: Any, root_category: str) -> str | None:
    if root_category == CONSOLE_CATEGORY:
        return "Полный комплект" if str(form.get("console_complete") or "yes") == "yes" else "Некомплект"
    return clean_optional(form.get("completeness"))


def item_condition_from_form(form: Any, root_category: str) -> str | None:
    if root_category == CONSOLE_CATEGORY:
        return clean_optional(form.get("console_condition"))
    if root_category == ACCESSORY_CATEGORY:
        value = str(form.get("visible_wear") or "no")
        return "Есть видимые следы использования" if value == "yes" else "Без видимых следов использования"
    return None


def infer_accessory_type(title: str) -> str:
    lowered = title.casefold()
    gamepad_terms = ("геймпад", "джойстик", "джостик", "dualshock", "dualsense", "controller")
    if any(term in lowered for term in gamepad_terms):
        return "Геймпад"
    return "Другой аксессуар"


def infer_item_category_name(title: str) -> str | None:
    lowered = title.casefold().replace("ё", "е")
    accessory_terms = (
        "dualshock",
        "dualsense",
        "геймпад",
        "джойстик",
        "джостик",
        "hdmi",
        "шдмай",
        "кабель",
        "провод",
        "заряд",
        "подстав",
        "гарнитур",
        "наушник",
        "камера",
        "пульт",
    )
    if any(term in lowered for term in accessory_terms):
        return ACCESSORY_CATEGORY
    if re.search(r"\b(ps4|ps5)\b", lowered) or "playstation 4" in lowered or "playstation 5" in lowered:
        return CONSOLE_CATEGORY
    return None


def item_condition_text(item: Item) -> str:
    if item.game_detail:
        return " ".join([item.game_detail.disc_surface or "", item.game_detail.test_result or ""])
    if item.console_detail:
        return item.console_detail.condition or ""
    if item.accessory_detail:
        return item.accessory_detail.condition or ""
    return ""


def item_completeness_text(item: Item) -> str:
    if item.game_detail:
        return item.game_detail.completeness or ""
    if item.console_detail:
        return " ".join([item.console_detail.completeness or "", item.console_detail.comment or ""])
    if item.accessory_detail:
        return item.accessory_detail.comment or ""
    return ""


def inventory_filter_options(items: list[Item]) -> dict[str, list[str]]:
    return {
        "category_filter": unique_inventory_options(items, lambda item: item.category.name),
        "condition_filter": unique_inventory_options(items, item_condition_text),
        "completeness_filter": unique_inventory_options(items, item_completeness_text),
    }


def unique_inventory_options(items: list[Item], extractor: Any) -> list[str]:
    values: dict[str, str] = {}
    for item in items:
        raw_value = extractor(item)
        if raw_value is None:
            continue
        value = " ".join(str(raw_value).split())
        if not value or value == "—":
            continue
        values.setdefault(value.casefold(), value)
    return sorted(values.values(), key=str.casefold)


def inventory_search_suggestions(items: list[Item]) -> list[dict[str, str]]:
    values: list[str] = []
    for item in items[:30]:
        values.extend(
            [
                item_display_name(item),
                item_short_code(item),
                item.category.name,
                item_platform_or_model(item),
            ]
        )
    values.extend(label for label, _aliases, _category in ITEM_TITLE_SUGGESTIONS)

    suggestions: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in values:
        clean = str(value or "").strip()
        key = suggestion_key(clean)
        if not clean or not key or key in seen:
            continue
        seen.add(key)
        suggestions.append({"label": clean, "search": clean.casefold()})
        if len(suggestions) >= 18:
            break
    return suggestions


def item_detail_search_text(item: Item) -> str:
    values: list[str] = []
    if item.game_detail:
        values.extend(
            [
                item.game_detail.interface_language or "",
                item.game_detail.subtitles_language or "",
                item.game_detail.voice_language or "",
                item.game_detail.disc_surface or "",
                item.game_detail.test_result or "",
                item.game_detail.completeness or "",
                item.game_detail.completeness_comment or "",
            ]
        )
    if item.console_detail:
        values.extend(
            [
                item.console_detail.model or "",
                item.console_detail.storage_size or "",
                str(item.console_detail.controllers_count or ""),
                item.console_detail.completeness or "",
                item.console_detail.condition or "",
                item.console_detail.comment or "",
            ]
        )
    if item.accessory_detail:
        values.extend(
            [
                item.accessory_detail.accessory_type or "",
                item.accessory_detail.platform or "",
                item.accessory_detail.originality or "",
                item.accessory_detail.condition or "",
                item.accessory_detail.comment or "",
            ]
        )
    return " ".join(values)


def load_item(db: Session, item_id: int) -> Item | None:
    return db.scalar(
        select(Item)
        .options(
            joinedload(Item.category),
            joinedload(Item.catalog_entry),
            joinedload(Item.game_detail),
            joinedload(Item.console_detail),
            joinedload(Item.accessory_detail),
        )
        .where(Item.id == item_id)
    )


def category_tree(db: Session) -> list[Category]:
    return list(db.scalars(select(Category).where(Category.parent_id.is_(None)).order_by(Category.name)))


def money(value: float | None) -> str:
    if value is None:
        return "неизвестно"
    return f"{value:,.0f} ₽".replace(",", " ")


def item_listing_price(item: Item, db: Session | None = None) -> float | None:
    if item.expected_sale_price is not None and item.expected_sale_price > 0:
        return item.expected_sale_price
    estimate = item_sale_price_estimate(item, db)
    if estimate is not None:
        price = round_price_nearest(float(estimate["expected_sell_price"]))
        minimum = minimum_sale_price(item.calculated_cost)
        if minimum is not None and price < minimum:
            return round_price_up(minimum)
        return price
    match = match_profit_knowledge_item(item)
    if match is None:
        return None
    base_price = match.base_market_price_rub or match.quick_sell_price_rub
    if base_price is None or base_price <= 0:
        return None
    price = round_price_nearest(base_price)
    minimum = minimum_sale_price(item.calculated_cost)
    if minimum is not None and price < minimum:
        return round_price_up(minimum)
    return price


def cost_label(value: float | None) -> str:
    if value == 0:
        return "Бесплатно"
    return money(value)


def datetime_label(value: datetime | None) -> str:
    if value is None:
        return "—"
    if value.hour == 0 and value.minute == 0 and value.second == 0:
        return value.strftime("%d.%m.%Y")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(DISPLAY_TIMEZONE)
    return value.strftime("%d.%m.%Y %H:%M")


def item_short_code(value: Item | str | None) -> str:
    code = value.code if isinstance(value, Item) else value
    if not code:
        return ""
    return str(code).removeprefix("ITEM-")


def item_age_days(item: Item) -> int | None:
    start_at = item.purchased_at or item.created_at
    if start_at is None:
        return None
    end_at = item.sold_at if item.status == "Продан" and item.sold_at else datetime.combine(date.today(), datetime.min.time())
    return max(0, (end_at.date() - start_at.date()).days)


def item_age_class(item: Item) -> str:
    days = item_age_days(item)
    if days is None:
        return "age-unknown"
    if days <= 14:
        return "age-fresh"
    if days <= 30:
        return "age-watch"
    return "age-urgent"


def item_age_title(item: Item) -> str:
    days = item_age_days(item)
    if days is None:
        return "Нет даты покупки"
    label = format_day_count(days)
    if item.status == "Продан":
        return f"Продался за {label}"
    return f"Лежит {label}"


def format_day_count(days: int) -> str:
    tail = abs(days) % 100
    last = abs(days) % 10
    if 11 <= tail <= 14:
        word = "дней"
    elif last == 1:
        word = "день"
    elif 2 <= last <= 4:
        word = "дня"
    else:
        word = "дней"
    return f"{days} {word}"


def utc_date() -> str:
    from app.models import utcnow

    return utcnow().date().isoformat()
