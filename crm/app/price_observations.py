"""Unified price observation layer for pricing analytics."""

from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.inventory import item_display_name, item_platform_or_model
from app.models import AnalysisItem, CatalogEntry, Item, MarketListing, MarketListingMatch, PriceObservation, utcnow
from app.suggestion_sources import normalize_lookup_text, profit_knowledge


OBS_OWN_SALE = "own_sale"
OBS_PARTNER_SALE = "partner_sale"
OBS_OWN_AVITO_ACTIVE = "own_avito_active"
OBS_AVITO_MARKET = "avito_market"
OBS_FALLBACK = "fallback"

OBSERVATION_LABELS = {
    OBS_OWN_SALE: "Твои продажи",
    OBS_PARTNER_SALE: "Партнёрские продажи",
    OBS_OWN_AVITO_ACTIVE: "Твои объявления Авито",
    OBS_AVITO_MARKET: "Рынок Авито",
    OBS_FALLBACK: "Fallback zip",
}

OBSERVATION_PRIORITY = {
    OBS_OWN_SALE: 2,
    OBS_PARTNER_SALE: 3,
    OBS_OWN_AVITO_ACTIVE: 4,
    OBS_AVITO_MARKET: 5,
    OBS_FALLBACK: 6,
}
BAD_AUTO_PRICE_ITEM_TYPES = {"digital_account", "subscription", "service", "account", "rent", "rental"}
BAD_AUTO_PRICE_TEXT = {"цифров", "аккаунт", "подпис", "аренд", "прокат", "услуг"}
AUTO_PRICE_SCOPES = {"per_item", "each_item_same_price"}
AUTO_PRICE_SOURCE_TYPES = {"listing_price_single_item", "description_item_price", "description_each_price"}
AUTO_PRICE_CONFIDENCE_LABELS = {"high", "medium"}


def price_observation_label(observation_type: str | None) -> str:
    return OBSERVATION_LABELS.get(observation_type or "", observation_type or "Источник")


def upsert_price_observation(
    session: Session,
    *,
    source: str,
    source_uid: str,
    observation_type: str,
    item_title: str,
    price: float | None,
    observed_at=None,
    confidence: float,
    usable_for_auto_price: bool = False,
    price_with_delivery: float | None = None,
    delivery_price: float | None = None,
    currency: str | None = "RUB",
    item_id: int | None = None,
    analysis_item_id: int | None = None,
    market_listing_id: int | None = None,
    catalog_entry_id: int | None = None,
    platform_or_model: str | None = None,
    reference_url: str | None = None,
    raw_json: dict[str, Any] | None = None,
) -> PriceObservation | None:
    normalized_price = safe_float(price)
    if normalized_price is None or normalized_price <= 0:
        return None
    clean_source = (source or observation_type or "unknown").strip()[:80]
    clean_uid = (source_uid or "").strip()[:255]
    if not clean_source or not clean_uid:
        return None

    row = pending_price_observation(session, clean_source, clean_uid)
    if row is None:
        row = session.scalar(
            select(PriceObservation).where(
                PriceObservation.source == clean_source,
                PriceObservation.source_uid == clean_uid,
            )
        )

    if row is None:
        row = PriceObservation(source=clean_source, source_uid=clean_uid, observation_type=observation_type, item_title=item_title)
        session.add(row)

    row.observation_type = observation_type
    row.item_id = item_id
    row.analysis_item_id = analysis_item_id
    row.market_listing_id = market_listing_id
    row.catalog_entry_id = catalog_entry_id
    row.item_title = item_title.strip() or "Без названия"
    row.platform_or_model = clean_optional(platform_or_model)
    row.price = normalized_price
    row.delivery_price = safe_float(delivery_price)
    row.price_with_delivery = safe_float(price_with_delivery)
    row.currency = (currency or "RUB").strip()[:20] or "RUB"
    row.observed_at = observed_at or utcnow()
    row.confidence = clamp(confidence, 0.0, 1.0)
    row.usable_for_auto_price = bool(usable_for_auto_price)
    row.reference_url = clean_optional(reference_url)
    row.raw_json = raw_json or None
    return row


def pending_price_observation(session: Session, source: str, source_uid: str) -> PriceObservation | None:
    for row in session.new:
        if isinstance(row, PriceObservation) and row.source == source and row.source_uid == source_uid:
            return row
    return None


def delete_price_observation(session: Session, *, source: str, source_uid: str) -> None:
    session.execute(
        delete(PriceObservation).where(
            PriceObservation.source == source,
            PriceObservation.source_uid == source_uid,
        )
    )


def delete_market_listing_observation(session: Session, listing_id: int) -> None:
    session.execute(delete(PriceObservation).where(PriceObservation.market_listing_id == listing_id))
    session.execute(delete(PriceObservation).where(PriceObservation.source_uid == market_listing_uid(listing_id)))


def record_own_sale_observation(session: Session, item: Item) -> PriceObservation | None:
    if item.status != "Продан" or item.sale_price is None:
        delete_price_observation(session, source="crm", source_uid=own_sale_uid(item.id))
        return None
    return upsert_price_observation(
        session,
        source="crm",
        source_uid=own_sale_uid(item.id),
        observation_type=OBS_OWN_SALE,
        item_id=item.id,
        catalog_entry_id=item.catalog_entry_id,
        item_title=item_display_name(item),
        platform_or_model=item_platform_or_model(item),
        price=item.sale_price,
        observed_at=item.sold_at or item.updated_at,
        confidence=1.0,
        usable_for_auto_price=True,
        raw_json={
            "item_type": item_observation_type(item),
            "cost": item.calculated_cost,
            "profit": item.sale_price - item.calculated_cost if item.calculated_cost is not None else None,
        },
    )


def record_partner_sale_observation(session: Session, item: AnalysisItem) -> PriceObservation | None:
    if item.status != "Продан" or item.sale_price is None:
        delete_price_observation(session, source=item.source_key, source_uid=partner_sale_uid(item))
        return None
    return upsert_price_observation(
        session,
        source=item.source_key,
        source_uid=partner_sale_uid(item),
        observation_type=OBS_PARTNER_SALE,
        analysis_item_id=item.id,
        item_title=item.title,
        platform_or_model=item.platform_or_model,
        price=item.sale_price,
        observed_at=item.sold_at or item.updated_at or item.imported_at,
        confidence=0.85,
        usable_for_auto_price=True,
        raw_json={
            "source_name": item.source_name,
            "source_item_code": item.source_item_code,
            "cost": item.calculated_cost,
            "profit": item.profit,
            "category": item.category,
            "details": item.details,
            "item_type": analysis_item_observation_type(item),
        },
    )


def item_observation_type(item: Item) -> str:
    if item.game_detail:
        return "game"
    if item.console_detail:
        return "console"
    category_text = normalize_lookup_text(item.category.name if item.category else "")
    if any(term in category_text for term in ("game", "игр")):
        return "game"
    if any(term in category_text for term in ("console", "пристав", "консоль")):
        return "console"
    if item.accessory_detail:
        text = normalize_lookup_text(
            " ".join(
                str(value or "")
                for value in (
                    item_display_name(item),
                    item.accessory_detail.accessory_type,
                    item.accessory_detail.originality,
                    item.accessory_detail.platform,
                )
            )
        )
        if any(term in text for term in ("controller", "gamepad", "dualshock", "dualsense", "джоист", "джостик", "геймпад")):
            return "controller"
        return "accessory"
    if any(term in category_text for term in ("accessory", "аксессуар")):
        return "accessory"
    return "unknown"


def analysis_item_observation_type(item: AnalysisItem) -> str:
    details = item.details if isinstance(item.details, dict) else {}
    text = normalize_lookup_text(
        " ".join(
            str(value or "")
            for value in (
                item.title,
                item.category,
                item.platform_or_model,
                details.get("accessory_type"),
                details.get("model"),
                details.get("platform"),
            )
        )
    )
    if any(term in text for term in ("controller", "gamepad", "dualshock", "dualsense", "джоист", "джостик", "геймпад")):
        return "controller"
    if any(term in text for term in ("console", "пристав", "консоль", "playstation 4 slim", "playstation 4 fat", "playstation 4 pro", "playstation 5 slim", "playstation 5 fat", "playstation 5 pro")):
        return "console"
    if any(term in text for term in ("game", "disc", "disk", "игр", "диск", "ps4", "ps5", "playstation 4", "playstation 5")):
        return "game"
    if any(term in text for term in ("accessory", "аксессуар")):
        return "accessory"
    return "unknown"


def record_market_listing_observation(session: Session, listing: MarketListing) -> PriceObservation | None:
    if listing.id is None:
        session.flush()
    delete_market_listing_observation(session, int(listing.id))
    session.flush()

    price = safe_float(listing.price)
    raw_json = listing.raw_json if isinstance(listing.raw_json, dict) else {}
    source = str(raw_json.get("source") or "adb_bot").strip() or "adb_bot"

    llm_market_rows = llm_market_price_observations(raw_json)
    if llm_market_rows is not None:
        created: list[PriceObservation] = []
        for index, payload in enumerate(llm_market_rows):
            row = record_llm_market_price_observation(session, listing, payload, source=source, index=index)
            if row is not None:
                created.append(row)
        return created[0] if created else None

    if price is None or price <= 0 or not can_use_listing_price_for_market_pricing(raw_json):
        return None

    extracted_item = single_llm_extracted_item(raw_json)
    payload = listing_price_observation_payload(listing, raw_json, extracted_item)
    delivery_price = market_delivery_price(raw_json)
    return upsert_price_observation(
        session,
        source=source,
        source_uid=f"{market_listing_uid(int(listing.id))}:listing-price",
        observation_type=OBS_AVITO_MARKET,
        market_listing_id=listing.id,
        catalog_entry_id=catalog_entry_id_for_price_payload(session, payload) or latest_listing_catalog_entry_id(session, int(listing.id)),
        item_title=price_payload_title(payload) or listing.title or listing.external_id or f"Объявление {listing.id}",
        platform_or_model=str(payload.get("platform") or listing.platform or "") or None,
        price=price,
        delivery_price=delivery_price,
        price_with_delivery=price + delivery_price if delivery_price is not None else None,
        currency=listing.currency or "RUB",
        observed_at=listing.scraped_at or listing.last_seen_at,
        confidence=listing_price_confidence(listing),
        usable_for_auto_price=market_price_payload_is_usable(payload),
        reference_url=listing.url,
        raw_json=payload,
    )


def record_llm_market_price_observation(
    session: Session,
    listing: MarketListing,
    payload: dict[str, Any],
    *,
    source: str,
    index: int,
) -> PriceObservation | None:
    normalized = normalized_llm_market_price_payload(payload, listing)
    price = safe_float(normalized.get("price_rub") or normalized.get("price"))
    if price is None or price <= 0:
        return None
    delivery_price = market_delivery_price(listing.raw_json if isinstance(listing.raw_json, dict) else {})
    return upsert_price_observation(
        session,
        source=source,
        source_uid=f"{market_listing_uid(int(listing.id))}:llm-market:{index}",
        observation_type=OBS_AVITO_MARKET,
        market_listing_id=listing.id,
        catalog_entry_id=catalog_entry_id_for_price_payload(session, normalized) or latest_listing_catalog_entry_id(session, int(listing.id)),
        item_title=price_payload_title(normalized) or listing.title or listing.external_id or f"Объявление {listing.id}",
        platform_or_model=str(normalized.get("platform") or listing.platform or "") or None,
        price=price,
        delivery_price=delivery_price,
        price_with_delivery=price + delivery_price if delivery_price is not None else None,
        currency=listing.currency or "RUB",
        observed_at=listing.scraped_at or listing.last_seen_at,
        confidence=price_confidence_score(normalized.get("price_confidence")),
        usable_for_auto_price=market_price_payload_is_usable(normalized),
        reference_url=listing.url,
        raw_json=normalized,
    )


def llm_market_price_observations(raw_json: dict[str, Any]) -> list[dict[str, Any]] | None:
    if "llm_market_price_observations" not in raw_json:
        return None
    value = raw_json.get("llm_market_price_observations")
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def normalized_llm_market_price_payload(payload: dict[str, Any], listing: MarketListing) -> dict[str, Any]:
    normalized = dict(payload)
    normalized.setdefault("source_kind", "llm_market_price_observation")
    normalized.setdefault("price_scope", "per_item")
    normalized.setdefault("price_source_type", "description_item_price")
    normalized.setdefault("price_confidence", "medium")
    normalized.setdefault("item_type", "game")
    normalized.setdefault("format", "physical")
    normalized.setdefault("listing_id", listing.id)
    normalized.setdefault("listing_title", listing.title)
    normalized.setdefault("listing_external_id", listing.external_id)
    normalized.setdefault("listing_url", listing.url)
    listing_raw = listing.raw_json if isinstance(listing.raw_json, dict) else {}
    if isinstance(listing_raw.get("monitor_context"), dict):
        normalized.setdefault("monitor_context", listing_raw["monitor_context"])
    for key in (
        "reservation_status",
        "posted_at",
        "posted_at_text",
        "is_reserved",
        "is_active",
        "finish_time",
        "avito_server_date",
    ):
        if listing_raw.get(key) is not None:
            normalized.setdefault(key, listing_raw.get(key))
    return normalized


def can_use_listing_price_for_market_pricing(raw_json: dict[str, Any]) -> bool:
    interpretation = raw_json.get("llm_listing_price_interpretation")
    if not isinstance(interpretation, dict):
        return False
    return (
        interpretation.get("role") == "single_item_price"
        and interpretation.get("can_use_listing_price_for_market_pricing") is True
        and single_llm_extracted_item(raw_json) is not None
    )


def single_llm_extracted_item(raw_json: dict[str, Any]) -> dict[str, Any] | None:
    rows = raw_json.get("llm_extracted_items")
    if not isinstance(rows, list):
        return None
    concrete = [row for row in rows if isinstance(row, dict) and price_payload_title(row)]
    return concrete[0] if len(concrete) == 1 else None


def listing_price_observation_payload(listing: MarketListing, raw_json: dict[str, Any], extracted_item: dict[str, Any] | None) -> dict[str, Any]:
    interpretation = raw_json.get("llm_listing_price_interpretation")
    payload = dict(extracted_item or {})
    payload.setdefault("name", listing.title)
    payload.setdefault("platform", listing.platform)
    payload.setdefault("item_type", payload.get("type") or "game")
    payload.setdefault("format", listing.format or "physical")
    payload["price_scope"] = "per_item"
    payload["price_source_type"] = "listing_price_single_item"
    payload.setdefault("price_confidence", "medium")
    payload["source_kind"] = "listing_price_single_item"
    if isinstance(interpretation, dict):
        payload["llm_listing_price_interpretation"] = interpretation
    payload["listing_id"] = listing.id
    payload["listing_title"] = listing.title
    payload["listing_external_id"] = listing.external_id
    payload["listing_url"] = listing.url
    if isinstance(raw_json.get("monitor_context"), dict):
        payload["monitor_context"] = raw_json["monitor_context"]
    for key in (
        "reservation_status",
        "posted_at",
        "posted_at_text",
        "is_reserved",
        "is_active",
        "finish_time",
        "avito_server_date",
    ):
        if raw_json.get(key) is not None:
            payload[key] = raw_json.get(key)
    return payload


def listing_price_confidence(listing: MarketListing) -> float:
    return 0.62 if listing.seller_type == "person" and not listing.is_shop else 0.52


def price_payload_title(payload: dict[str, Any]) -> str | None:
    for key in ("canonical_name", "name", "title"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return None


def catalog_entry_id_for_price_payload(session: Session, payload: dict[str, Any]) -> int | None:
    numeric_id = safe_int(payload.get("catalog_entry_id"))
    if numeric_id and session.get(CatalogEntry, numeric_id) is not None:
        return int(numeric_id)
    terms: list[str] = []
    for key in ("catalog_item_id", "canonical_name", "name", "title"):
        value = str(payload.get(key) or "").strip()
        if value:
            terms.append(normalize_lookup_text(value.replace("_", " ")))
    terms = [term for term in dict.fromkeys(terms) if term]
    if not terms:
        return None
    for entry in session.scalars(select(CatalogEntry).options(joinedload(CatalogEntry.aliases))).unique():
        names = [entry.name] + [alias.name for alias in entry.aliases]
        normalized_names = {normalize_lookup_text(name) for name in names}
        if any(term in normalized_names for term in terms):
            return int(entry.id)
    return None


def market_price_payload_is_usable(payload: dict[str, Any]) -> bool:
    item_type = normalize_lookup_text(str(payload.get("item_type") or payload.get("type") or ""))
    if item_type in BAD_AUTO_PRICE_ITEM_TYPES or any(token in item_type for token in BAD_AUTO_PRICE_TEXT):
        return False
    canonical_name = normalize_lookup_text(str(payload.get("canonical_name") or ""))
    catalog_item_id = normalize_lookup_text(str(payload.get("catalog_item_id") or payload.get("catalog_entry_id") or ""))
    if canonical_name == "1" or catalog_item_id == "1":
        return False
    if item_type == "game" and canonical_name == "playstation 4":
        return False
    title_text = normalize_lookup_text(
        " ".join(
            str(payload.get(key) or "")
            for key in ("name", "canonical_name", "title", "listing_title", "price_source_text")
        )
    )
    if item_type == "console" and not looks_like_console_price_title(title_text):
        return False
    if item_type in {"controller", "accessory"} and not looks_like_accessory_price_title(title_text):
        return False
    listing_format = normalize_lookup_text(str(payload.get("format") or ""))
    if "digital" in listing_format or "цифр" in listing_format:
        return False
    if str(payload.get("price_scope") or "") not in AUTO_PRICE_SCOPES:
        return False
    if str(payload.get("price_source_type") or "") not in AUTO_PRICE_SOURCE_TYPES:
        return False
    numeric_confidence = safe_float(payload.get("price_confidence"))
    if numeric_confidence is not None:
        return numeric_confidence >= 0.6
    confidence = str(payload.get("price_confidence") or "").casefold()
    return confidence in AUTO_PRICE_CONFIDENCE_LABELS


def looks_like_console_price_title(text: str) -> bool:
    if not text:
        return False
    console_markers = (
        "playstation 4",
        "playstation 5",
        "ps4",
        "ps 4",
        "ps5",
        "ps 5",
        "пс4",
        "пс 4",
        "пс5",
        "пс 5",
        "пристав",
        "консол",
    )
    bad_markers = (
        "fifa",
        "gta",
        "god of war",
        "mortal",
        "spider",
        "человек паук",
        "диск",
        "игра",
        "game stick",
        "psp",
        "portable",
    )
    return any(marker in text for marker in console_markers) and not any(marker in text for marker in bad_markers)


def looks_like_accessory_price_title(text: str) -> bool:
    if not text:
        return False
    accessory_markers = (
        "dualshock",
        "dual shock",
        "dualsense",
        "dual sense",
        "controller",
        "gamepad",
        "геймпад",
        "джойст",
        "джост",
        "контроллер",
        "заряд",
        "camera",
        "камера",
        "portal",
        "vr",
        "psvr",
    )
    bad_markers = (
        "ведьмак",
        "witcher",
        "диск для прошивки",
        "кейс для диска",
        "коробка",
        "бокс",
        "playstation 4 pro",
        "ps4 pro",
    )
    return any(marker in text for marker in accessory_markers) and not any(marker in text for marker in bad_markers)


def price_confidence_score(value: Any) -> float:
    numeric = safe_float(value)
    if numeric is not None:
        return clamp(numeric, 0.0, 1.0)
    label = str(value or "").casefold()
    if label == "high":
        return 0.75
    if label == "medium":
        return 0.65
    if label == "low":
        return 0.45
    return 0.6


def safe_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def price_observation_is_usable_for_auto_price(observation: PriceObservation) -> bool:
    if not observation.usable_for_auto_price:
        return False
    if observation.observation_type != OBS_AVITO_MARKET:
        return True
    raw_json = observation.raw_json if isinstance(observation.raw_json, dict) else {}
    return market_price_payload_is_usable(raw_json)


def record_fallback_observation(session: Session, item: Item) -> PriceObservation | None:
    if item.status == "Продан":
        return None
    match = match_fallback_item(item)
    if match is None:
        return None
    price, price_basis = fallback_price(match)
    if price is None:
        return None
    return upsert_price_observation(
        session,
        source="crm_profit_knowledge.zip",
        source_uid=f"fallback:item:{item.id}:{match.id}",
        observation_type=OBS_FALLBACK,
        item_id=item.id,
        catalog_entry_id=item.catalog_entry_id,
        item_title=item_display_name(item),
        platform_or_model=item_platform_or_model(item),
        price=price,
        observed_at=item.updated_at or item.created_at,
        confidence=fallback_confidence(match.price_confidence),
        usable_for_auto_price=True,
        raw_json={
            "matched_name": match.canonical_name,
            "matched_id": match.id,
            "price_source": match.price_source,
            "price_confidence": match.price_confidence,
            "price_basis": price_basis,
            "net_after_sale_rub": match.net_after_sale_rub,
            "good_net_after_sale_rub": match.good_net_after_sale_rub,
            "bad_net_after_sale_rub": match.bad_net_after_sale_rub,
            "base_market_price_rub": match.base_market_price_rub,
            "quick_sell_price_rub": match.quick_sell_price_rub,
        },
    )


def sync_price_observations(session: Session, *, include_fallback: bool = True) -> None:
    items = list(
        session.scalars(
            select(Item).options(
                selectinload(Item.category),
                selectinload(Item.catalog_entry),
                selectinload(Item.game_detail),
                selectinload(Item.console_detail),
                selectinload(Item.accessory_detail),
            )
        )
    )
    for item in items:
        record_own_sale_observation(session, item)
        if include_fallback:
            record_fallback_observation(session, item)

    partner_items = list(session.scalars(select(AnalysisItem)))
    for item in partner_items:
        record_partner_sale_observation(session, item)

    market_listings = list(session.scalars(select(MarketListing).options(joinedload(MarketListing.matches))).unique())
    for listing in market_listings:
        record_market_listing_observation(session, listing)


def price_observation_summary(session: Session, *, recent_limit: int = 12) -> dict[str, Any]:
    rows = list(session.scalars(select(PriceObservation).order_by(PriceObservation.observed_at.desc(), PriceObservation.id.desc())))
    by_type = Counter(row.observation_type for row in rows)
    usable_count = sum(1 for row in rows if row.usable_for_auto_price)
    return {
        "total": len(rows),
        "usable_count": usable_count,
        "analytics_only_count": len(rows) - usable_count,
        "by_type": [
            {
                "type": observation_type,
                "label": OBSERVATION_LABELS.get(observation_type, observation_type),
                "count": by_type.get(observation_type, 0),
            }
            for observation_type in sorted(by_type, key=lambda value: OBSERVATION_PRIORITY.get(value, 99))
        ],
        "recent": rows[:recent_limit],
        "latest_at": max((row.observed_at for row in rows), default=None),
        "average_confidence": session.scalar(select(func.avg(PriceObservation.confidence))) or 0,
    }


def own_sale_uid(item_id: int) -> str:
    return f"own-sale:item:{item_id}"


def partner_sale_uid(item: AnalysisItem) -> str:
    return f"partner-sale:{item.source_key}:{item.source_item_code}"


def market_listing_uid(listing_id: int) -> str:
    return f"market-listing:{listing_id}"


def latest_listing_catalog_entry_id(session: Session, listing_id: int) -> int | None:
    match = session.scalar(
        select(MarketListingMatch)
        .where(MarketListingMatch.market_listing_id == listing_id, MarketListingMatch.catalog_entry_id.is_not(None))
        .order_by(MarketListingMatch.id.desc())
    )
    return match.catalog_entry_id if match is not None else None


def market_delivery_price(raw_json: dict[str, Any]) -> float | None:
    for key in ("delivery_price_rub", "delivery_price"):
        value = safe_float(raw_json.get(key))
        if value is not None and value >= 0:
            return value
    return None


def match_fallback_item(item: Item):
    knowledge = profit_knowledge()
    if not knowledge.items_by_id:
        return None
    allowed_types = fallback_item_types(item)
    if not allowed_types:
        return None
    terms = [normalize_lookup_text(term) for term in fallback_lookup_terms(item)]
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


def fallback_item_types(item: Item) -> set[str]:
    if item.game_detail:
        return {"game"}
    if item.console_detail:
        return {"console"}
    if item.accessory_detail:
        text = normalize_lookup_text(" ".join(fallback_lookup_terms(item)))
        if any(term in text for term in ("dualshock", "dualsense", "controller", "джоистик", "джостик", "геймпад")):
            return {"controller"}
    return set()


def fallback_lookup_terms(item: Item) -> list[str]:
    terms = [item_display_name(item), item_platform_or_model(item)]
    if item.game_detail:
        terms.extend(
            [
                item.game_detail.platform,
                item.game_detail.completeness_comment,
                item.game_detail.interface_language,
                item.game_detail.subtitles_language,
                item.game_detail.voice_language,
            ]
        )
    elif item.console_detail:
        terms.extend([item.console_detail.model, item.console_detail.storage_size])
    elif item.accessory_detail:
        terms.extend([item.accessory_detail.accessory_type, item.accessory_detail.platform, item.accessory_detail.originality])
    return [str(term) for term in terms if term]


def fallback_price(match: Any) -> tuple[float | None, str]:
    options = (
        ("base_market_price_rub", match.base_market_price_rub),
        ("quick_sell_price_rub", match.quick_sell_price_rub),
        ("net_after_sale_rub", match.net_after_sale_rub),
        ("good_net_after_sale_rub", match.good_net_after_sale_rub),
        ("bad_net_after_sale_rub", match.bad_net_after_sale_rub),
    )
    for key, value in options:
        price = safe_float(value)
        if price is not None and price > 0:
            return price, key
    return None, "unknown"


def fallback_confidence(value: str | None) -> float:
    return {
        "high": 0.45,
        "medium": 0.35,
        "manual": 0.30,
        "low": 0.20,
    }.get((value or "").strip(), 0.25)


def clean_optional(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace("\xa0", " ").replace("₽", "").replace("руб.", "").replace("руб", "").replace(",", ".")
    text = "".join(char for char in text if char.isdigit() or char in ".-")
    if not text or text in {"-", ".", "-."}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, float(value)))
