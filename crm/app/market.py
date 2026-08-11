"""Local market JSON import, matching, and median price estimates."""

from __future__ import annotations

import glob
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, joinedload

from app.models import CatalogAlias, CatalogEntry, MarketImport, MarketListing, MarketListingMatch, utcnow
from app.price_observations import record_market_listing_observation
from app.schemas import extract_platform, extract_seller_user_key, normalize_to_utc_naive, parse_datetime, parse_price


MATCH_AUTO = "Автоматически"
MATCH_CONFIRMED = "Подтверждено"
MATCH_AMBIGUOUS = "Неоднозначно"
MATCH_EXCLUDED = "Исключено"

STOP_WORDS = {
    "диск",
    "игра",
    "игры",
    "ps4",
    "ps5",
    "playstation",
    "продам",
    "обмен",
    "торг",
}


@dataclass
class MarketImportStats:
    files_count: int = 0
    raw_count: int = 0
    unique_count: int = 0
    duplicate_count: int = 0
    physical_count: int = 0
    digital_count: int = 0
    subscription_count: int = 0
    private_count: int = 0
    company_count: int = 0
    auto_matched_count: int = 0
    ambiguous_count: int = 0


@dataclass
class MarketEstimate:
    count: int
    min_price: float | None
    median_price: float | None
    max_price: float | None
    last_updated_at: Any
    confidence: str
    used_private_only: bool


@dataclass
class AvitoBatchImportResult:
    import_id: int | None
    raw_count: int
    unique_count: int
    duplicate_count: int
    created_listing_ids: list[int]
    duplicate_listing_ids: list[int]


def expand_input_paths(patterns: list[str]) -> list[Path]:
    paths: list[Path] = []
    for pattern in patterns:
        matches = glob.glob(pattern)
        if matches:
            paths.extend(Path(match) for match in matches)
        else:
            paths.append(Path(pattern))
    unique: dict[str, Path] = {}
    for path in paths:
        if path.exists() and path.is_file():
            unique[str(path.resolve())] = path
    return list(unique.values())


def import_market_files(session: Session, patterns: list[str]) -> MarketImportStats:
    paths = expand_input_paths(patterns)
    stats = MarketImportStats(files_count=len(paths))
    for path in paths:
        rows = _read_json_array(path)
        file_seen: set[str] = set()
        file_unique = 0
        file_duplicates = 0
        for raw in rows:
            if not isinstance(raw, dict):
                raw = {"value": raw}
            stats.raw_count += 1
            external_id = str(raw.get("id") or "").strip()
            if not external_id:
                continue
            duplicate_in_file = external_id in file_seen
            existing = session.scalar(select(MarketListing).where(MarketListing.external_id == external_id))
            if duplicate_in_file or existing is not None:
                stats.duplicate_count += 1
                file_duplicates += 1
            else:
                file_unique += 1
            file_seen.add(external_id)
            listing = upsert_market_listing(session, raw)
            update_market_match(session, listing)
            record_market_listing_observation(session, listing)

        import_row = MarketImport(
            filename=str(path),
            raw_count=len(rows),
            unique_count=file_unique,
            duplicate_count=file_duplicates,
        )
        session.add(import_row)

    session.flush()
    stats.unique_count = session.scalar(select(func.count(MarketListing.id))) or 0
    stats.physical_count = session.scalar(select(func.count(MarketListing.id)).where(MarketListing.format == "Физический")) or 0
    stats.digital_count = session.scalar(select(func.count(MarketListing.id)).where(MarketListing.format == "Цифровой")) or 0
    stats.subscription_count = session.scalar(select(func.count(MarketListing.id)).where(MarketListing.type == "Подписка")) or 0
    stats.private_count = session.scalar(select(func.count(MarketListing.id)).where(MarketListing.seller_type == "person")) or 0
    stats.company_count = session.scalar(select(func.count(MarketListing.id)).where(MarketListing.seller_type == "company")) or 0
    stats.auto_matched_count = session.scalar(
        select(func.count(MarketListingMatch.id)).where(MarketListingMatch.status == MATCH_AUTO)
    ) or 0
    stats.ambiguous_count = session.scalar(
        select(func.count(MarketListingMatch.id)).where(MarketListingMatch.status == MATCH_AMBIGUOUS)
    ) or 0
    return stats


def import_avito_api_batch(
    session: Session,
    *,
    source: str | None,
    filename: str,
    scraped_at: datetime | None,
    listings: list[dict[str, Any]],
    crm_sent_at: datetime | None = None,
) -> AvitoBatchImportResult:
    now = utcnow()
    batch_scraped_at = normalize_datetime(scraped_at)
    batch_sent_at = normalize_datetime(crm_sent_at) or batch_scraped_at
    listing_fallback_scraped_at = batch_scraped_at or batch_sent_at or now
    import_row = MarketImport(
        filename=filename or source or "adb_bot_live",
        imported_at=now,
        scraped_at=batch_scraped_at,
        crm_sent_at=batch_sent_at,
        raw_count=0,
        unique_count=0,
        duplicate_count=0,
    )
    session.add(import_row)
    session.flush()

    created_listing_ids: list[int] = []
    duplicate_listing_ids: list[int] = []
    known_external_ids: dict[str, MarketListing] = {}
    known_urls: dict[str, MarketListing] = {}
    known_fingerprints: dict[str, MarketListing] = {}

    for raw in listings:
        listing = find_existing_api_listing(
            session,
            raw,
            known_external_ids=known_external_ids,
            known_urls=known_urls,
            known_fingerprints=known_fingerprints,
        )
        if listing is None:
            listing = create_api_market_listing(raw, source=source, scraped_at=listing_fallback_scraped_at, now=now)
            session.add(listing)
            session.flush()
            created_listing_ids.append(int(listing.id))
            import_row.raw_count += 1
            import_row.unique_count += 1
        else:
            update_api_market_listing(listing, raw, source=source, scraped_at=listing_fallback_scraped_at, now=now)
            duplicate_listing_ids.append(int(listing.id))
        record_market_listing_observation(session, listing)
        remember_api_listing(
            listing,
            known_external_ids=known_external_ids,
            known_urls=known_urls,
            known_fingerprints=known_fingerprints,
        )

    if import_row.unique_count <= 0:
        session.delete(import_row)
        import_id = None
    else:
        import_id = int(import_row.id)
    session.flush()
    return AvitoBatchImportResult(
        import_id=import_id,
        raw_count=len(created_listing_ids),
        unique_count=len(created_listing_ids),
        duplicate_count=len(duplicate_listing_ids),
        created_listing_ids=created_listing_ids,
        duplicate_listing_ids=duplicate_listing_ids,
    )


def find_existing_api_listing(
    session: Session,
    raw: dict[str, Any],
    *,
    known_external_ids: dict[str, MarketListing],
    known_urls: dict[str, MarketListing],
    known_fingerprints: dict[str, MarketListing],
) -> MarketListing | None:
    external_id = api_external_id(raw)
    url = clean(raw.get("url"))
    fingerprint = api_fingerprint(raw)
    if external_id and external_id in known_external_ids:
        return known_external_ids[external_id]
    if url and url in known_urls:
        return known_urls[url]
    if fingerprint and fingerprint in known_fingerprints:
        return known_fingerprints[fingerprint]
    if external_id:
        listing = session.scalar(select(MarketListing).where(MarketListing.external_id == external_id))
        if listing is not None:
            return listing
    if url:
        listing = session.scalar(select(MarketListing).where(MarketListing.url == url))
        if listing is not None:
            return listing
    if fingerprint:
        for listing in session.scalars(select(MarketListing).where(MarketListing.raw_json.is_not(None))):
            raw_json = listing.raw_json if isinstance(listing.raw_json, dict) else {}
            if str(raw_json.get("fingerprint") or "").strip() == fingerprint:
                return listing
    return None


def create_api_market_listing(
    raw: dict[str, Any],
    *,
    source: str | None,
    scraped_at: datetime,
    now: datetime,
) -> MarketListing:
    external_id = api_external_id(raw) or fallback_external_id(raw)
    if not external_id:
        raise ValueError("Market listing needs external_id, url or raw_json.fingerprint")
    return MarketListing(
        external_id=external_id,
        bot_listing_id=clean(raw.get("bot_listing_id")) or external_id,
        title=clean(raw.get("title")),
        description=clean(raw.get("description")),
        url=clean(raw.get("url")),
        price=parse_price(raw.get("price")),
        currency=clean(raw.get("currency")) or "RUB",
        address=clean(raw.get("address")),
        platform=clean(raw.get("platform")),
        format=clean(raw.get("format")),
        type=clean(raw.get("type")),
        localization=clean(raw.get("localization")),
        seller_name=clean(raw.get("seller_name")),
        seller_user_key=clean(raw.get("seller_user_key")),
        seller_type=clean(raw.get("seller_type")),
        is_shop=bool(raw.get("is_shop")),
        posted_at=parse_datetime(raw.get("posted_at")),
        scraped_at=parse_datetime(raw.get("scraped_at")) or scraped_at,
        raw_json=api_raw_json(raw, source=source),
        first_seen_at=now,
        last_seen_at=now,
    )


def update_api_market_listing(
    listing: MarketListing,
    raw: dict[str, Any],
    *,
    source: str | None,
    scraped_at: datetime,
    now: datetime,
) -> None:
    update_if_not_empty(listing, "bot_listing_id", clean(raw.get("bot_listing_id")) or api_external_id(raw))
    update_if_not_empty(listing, "title", clean(raw.get("title")))
    update_if_not_empty(listing, "description", clean(raw.get("description")))
    update_if_not_empty(listing, "url", clean(raw.get("url")))
    update_if_not_none(listing, "price", parse_price(raw.get("price")))
    update_if_not_empty(listing, "currency", clean(raw.get("currency")) or "RUB")
    update_if_not_empty(listing, "address", clean(raw.get("address")))
    update_if_not_empty(listing, "platform", clean(raw.get("platform")))
    update_if_not_empty(listing, "format", clean(raw.get("format")))
    update_if_not_empty(listing, "type", clean(raw.get("type")))
    update_if_not_empty(listing, "localization", clean(raw.get("localization")))
    update_if_not_empty(listing, "seller_name", clean(raw.get("seller_name")))
    update_if_not_empty(listing, "seller_user_key", clean(raw.get("seller_user_key")))
    update_if_not_empty(listing, "seller_type", clean(raw.get("seller_type")))
    if raw.get("is_shop") is not None:
        listing.is_shop = bool(raw.get("is_shop"))
    update_if_not_none(listing, "posted_at", parse_datetime(raw.get("posted_at")))
    listing.scraped_at = parse_datetime(raw.get("scraped_at")) or scraped_at
    raw_json = api_raw_json(raw, source=source)
    if raw_json:
        listing.raw_json = raw_json
    listing.last_seen_at = now


def remember_api_listing(
    listing: MarketListing,
    *,
    known_external_ids: dict[str, MarketListing],
    known_urls: dict[str, MarketListing],
    known_fingerprints: dict[str, MarketListing],
) -> None:
    if listing.external_id:
        known_external_ids[listing.external_id] = listing
    if listing.url:
        known_urls[listing.url] = listing
    raw_json = listing.raw_json if isinstance(listing.raw_json, dict) else {}
    fingerprint = str(raw_json.get("fingerprint") or "").strip()
    if fingerprint:
        known_fingerprints[fingerprint] = listing


def api_external_id(raw: dict[str, Any]) -> str | None:
    return clean(raw.get("external_id") or raw.get("id"))


def api_fingerprint(raw: dict[str, Any]) -> str | None:
    raw_json = raw.get("raw_json")
    if not isinstance(raw_json, dict):
        return None
    return clean(raw_json.get("fingerprint"))


def api_raw_json(raw: dict[str, Any], *, source: str | None) -> dict[str, Any]:
    raw_json = raw.get("raw_json")
    payload = dict(raw_json) if isinstance(raw_json, dict) else {}
    if source and "source" not in payload:
        payload["source"] = source
    return payload


def fallback_external_id(raw: dict[str, Any]) -> str | None:
    url = clean(raw.get("url"))
    if url:
        return f"adb_url_{stable_digest(url)}"
    fingerprint = api_fingerprint(raw)
    if fingerprint:
        return f"adb_fp_{stable_digest(fingerprint)}"
    return None


def stable_digest(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:32]


def normalize_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return normalize_to_utc_naive(value)


def update_if_not_empty(listing: MarketListing, field: str, value: str | None) -> None:
    if value:
        setattr(listing, field, value)


def update_if_not_none(listing: MarketListing, field: str, value: Any) -> None:
    if value is not None:
        setattr(listing, field, value)


def upsert_market_listing(session: Session, raw: dict[str, Any]) -> MarketListing:
    external_id = str(raw.get("id") or "").strip()
    if not external_id:
        raise ValueError("Market listing has no id")
    listing = session.scalar(select(MarketListing).where(MarketListing.external_id == external_id))
    now = utcnow()
    if listing is None:
        listing = MarketListing(
            external_id=external_id,
            bot_listing_id=str(raw.get("bot_listing_id") or external_id).strip() or external_id,
            first_seen_at=now,
            raw_json=raw,
        )
        session.add(listing)
    elif not listing.bot_listing_id:
        listing.bot_listing_id = str(raw.get("bot_listing_id") or external_id).strip() or external_id
    listing.title = clean(raw.get("title"))
    listing.description = clean(raw.get("description"))
    listing.url = clean(raw.get("url"))
    listing.price = parse_price(raw.get("price"))
    listing.currency = clean(raw.get("currency"))
    listing.address = clean(raw.get("address"))
    listing.platform = classify_platform(raw)
    listing.format = classify_format(raw)
    listing.type = classify_type(raw)
    listing.localization = classify_localization(raw)
    seller = raw.get("seller") if isinstance(raw.get("seller"), dict) else {}
    listing.seller_name = clean(seller.get("name") or seller.get("title"))
    listing.seller_user_key = extract_seller_user_key(seller)
    listing.seller_type = classify_seller_type(raw)
    listing.is_shop = bool(raw.get("isShop") or raw.get("is_shop") or listing.seller_type == "company")
    listing.posted_at = parse_datetime(raw.get("postedAt") or raw.get("postedTimestamp"))
    listing.scraped_at = parse_datetime(raw.get("scrapedAt")) or now
    listing.raw_json = raw
    listing.last_seen_at = now
    return listing


def update_market_match(session: Session, listing: MarketListing) -> MarketListingMatch:
    session.flush()
    existing = session.scalar(
        select(MarketListingMatch).where(MarketListingMatch.market_listing_id == listing.id).order_by(MarketListingMatch.id.desc())
    )
    if existing and existing.status in {MATCH_CONFIRMED, MATCH_EXCLUDED}:
        return existing
    if existing:
        session.delete(existing)
        session.flush()
    candidates = find_catalog_candidates(session, listing.title or "")
    if len(candidates) == 1:
        match = MarketListingMatch(
            market_listing_id=listing.id,
            catalog_entry_id=candidates[0].id,
            confidence=0.9,
            status=MATCH_AUTO,
        )
    elif len(candidates) > 1:
        match = MarketListingMatch(
            market_listing_id=listing.id,
            catalog_entry_id=None,
            confidence=0.3,
            status=MATCH_AMBIGUOUS,
        )
    else:
        match = MarketListingMatch(
            market_listing_id=listing.id,
            catalog_entry_id=None,
            confidence=0.0,
            status=MATCH_AMBIGUOUS,
        )
    session.add(match)
    return match


def find_catalog_candidates(session: Session, title: str) -> list[CatalogEntry]:
    normalized_title = normalize_text(title)
    if not normalized_title:
        return []
    candidates: list[CatalogEntry] = []
    entries = session.scalars(select(CatalogEntry).options(joinedload(CatalogEntry.aliases))).unique()
    for entry in entries:
        names = [entry.name] + [alias.name for alias in entry.aliases]
        for name in names:
            norm_name = normalize_text(name)
            if norm_name and _contains_words(normalized_title, norm_name):
                candidates.append(entry)
                break
    unique = {entry.id: entry for entry in candidates}
    return list(unique.values())


def market_estimate(
    session: Session,
    *,
    catalog_entry_id: int,
    platform: str | None = None,
) -> MarketEstimate:
    base = (
        select(MarketListing)
        .join(MarketListingMatch, MarketListingMatch.market_listing_id == MarketListing.id)
        .where(
            MarketListingMatch.catalog_entry_id == catalog_entry_id,
            MarketListingMatch.status.in_((MATCH_AUTO, MATCH_CONFIRMED)),
            MarketListing.format == "Физический",
            MarketListing.type == "Диск",
            MarketListing.price.is_not(None),
            MarketListing.price > 0,
        )
    )
    if platform:
        base = base.where(MarketListing.platform == platform)
    private_listings = list(
        session.scalars(base.where(MarketListing.seller_type == "person", MarketListing.is_shop.is_(False))).unique()
    )
    used_private_only = len(private_listings) >= 3
    listings = private_listings if used_private_only else list(session.scalars(base).unique())
    prices = sorted(float(item.price) for item in listings if item.price is not None and item.price > 0)
    if not prices:
        return MarketEstimate(0, None, None, None, None, "нет данных", used_private_only)
    count = len(prices)
    confidence = "низкий" if count <= 2 else "средний" if count <= 9 else "высокий"
    last_updated = max((item.last_seen_at for item in listings if item.last_seen_at), default=None)
    return MarketEstimate(
        count=count,
        min_price=prices[0],
        median_price=float(median(prices)),
        max_price=prices[-1],
        last_updated_at=last_updated,
        confidence=confidence,
        used_private_only=used_private_only,
    )


def confirm_market_match(session: Session, *, listing_id: int, catalog_entry_id: int | None, status: str) -> None:
    listing = session.get(MarketListing, listing_id)
    if listing is None:
        raise ValueError("Объявление не найдено")
    session.execute(delete(MarketListingMatch).where(MarketListingMatch.market_listing_id == listing_id))
    session.add(
        MarketListingMatch(
            market_listing_id=listing_id,
            catalog_entry_id=catalog_entry_id,
            confidence=1.0 if status == MATCH_CONFIRMED else 0.0,
            status=status,
        )
    )


def classify_platform(raw: dict[str, Any]) -> str | None:
    text = " ".join(str(raw.get(key) or "") for key in ("title", "description"))
    parameter_platform = extract_platform(raw.get("parameters"))
    if parameter_platform:
        if "5" in parameter_platform:
            return "PlayStation 5"
        if "4" in parameter_platform:
            return "PlayStation 4"
        return parameter_platform
    lower = text.lower()
    if "ps5" in lower or "playstation 5" in lower or "пс5" in lower:
        return "PlayStation 5"
    if "ps4" in lower or "playstation 4" in lower or "пс4" in lower:
        return "PlayStation 4"
    return None


def classify_format(raw: dict[str, Any]) -> str:
    title = f"{raw.get('title') or ''} {raw.get('description') or ''}".lower()
    if any(word in title for word in ("цифров", "аккаунт", "код", "ключ", "primary", "secondary")):
        return "Цифровой"
    return "Физический"


def classify_type(raw: dict[str, Any]) -> str:
    title = f"{raw.get('title') or ''} {raw.get('description') or ''}".lower()
    if any(word in title for word in ("подписк", "ps plus", "playstation plus", "plus deluxe", "extra")):
        return "Подписка"
    if any(word in title for word in ("комплект", "лот ", "набор")):
        return "Комплект"
    if classify_format(raw) == "Цифровой":
        return "Цифровой товар"
    return "Диск"


def classify_localization(raw: dict[str, Any]) -> str | None:
    text = f"{raw.get('title') or ''} {raw.get('description') or ''}".lower()
    if "рус" in text:
        return "Русский"
    if "англ" in text or "eng" in text:
        return "Английский"
    return None


def classify_seller_type(raw: dict[str, Any]) -> str:
    user_type = str(raw.get("userType") or raw.get("sellerType") or "").lower()
    seller = raw.get("seller") if isinstance(raw.get("seller"), dict) else {}
    seller_type = str(seller.get("type") or seller.get("userType") or "").lower()
    text = " ".join((user_type, seller_type, str(seller.get("name") or ""))).lower()
    if any(word in text for word in ("company", "shop", "business", "магаз", "компан")):
        return "company"
    return "person"


def normalize_text(text: str) -> str:
    lowered = text.lower()
    lowered = re.sub(r"[^\w\s]", " ", lowered, flags=re.UNICODE)
    words = [word for word in lowered.split() if word not in STOP_WORDS]
    return " ".join(words)


def _contains_words(haystack: str, needle: str) -> bool:
    hay_words = haystack.split()
    needle_words = needle.split()
    if not needle_words:
        return False
    for index in range(0, len(hay_words) - len(needle_words) + 1):
        if hay_words[index : index + len(needle_words)] == needle_words:
            return True
    return needle in haystack


def _read_json_array(path: Path) -> list[Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} должен содержать JSON-массив")
    return data


def clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None

