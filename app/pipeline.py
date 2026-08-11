from __future__ import annotations

import json
import re
from html import unescape
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


from .crm_market import (
    build_market_import_payload,
    build_market_listing_payload,
    evaluate_record_profit_if_configured,
    post_market_import,
)
from .listing_db import ImportStats, upsert_phone_record
from .llm_analyzer import extract_listing_items_if_configured, summarize_description_if_needed
from .main import (
    append_url_text,
    build_link_monitor_payload,
    compact_external_result,
    description_summary_threshold,
    listing_reservation_status,
    telegram_suppression_reason,
    truncate_description_for_telegram,
    write_link_monitor_payload,
)
from .monitor import (
    NewListingRecord,
    delivery_price_from_text,
    delivery_status_from_text,
    item_kind_from_title,
    seller_city_from_address,
)
from .notifier import send_listing_plain_if_configured, send_profit_evaluation_if_configured


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = PROJECT_ROOT / "data" / "automation_settings.json"
AVITO_LISTING_IMAGE_RE = re.compile(r"^https?://\d+\.img\.avito\.st/image/", re.IGNORECASE)
AVITO_IMAGE_TOKEN_RE = re.compile(r"/image/\d+/\d+\.([A-Za-z0-9_-]+)", re.IGNORECASE)
IMAGE_BLOCKLIST_TOKENS = (
    "static/",
    "sale-banner",
    "banner",
    "alfa",
    "logo",
    "icon",
    "avatar",
)
DB_PATH = PROJECT_ROOT / "data" / "parsed" / "avito_metrics.db"
GAME_PRICE_MAP_PATH = PROJECT_ROOT / "data" / "game_price_map.json"


def load_automation_settings(path: Path = SETTINGS_PATH) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def load_price_map(path: Path = GAME_PRICE_MAP_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"games": []}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"games": []}
    return loaded if isinstance(loaded, dict) else {"games": []}


def save_record_to_db(
    payload: dict[str, Any],
    *,
    source_path: str | Path | None = None,
) -> ImportStats:
    """Persist collected listing into the unified PostgreSQL CRM database.

    The old project wrote here into data/parsed/avito_metrics.db. That SQLite
    database is now treated as imported archive/history only; active bot data
    is stored in CRM PostgreSQL via the local market import API.
    """

    fake = type("RecordProxy", (), {})()
    for key, value in payload.items():
        setattr(fake, key, value)
    setattr(fake, "source", payload.get("source") or "avito_playwright_parser")
    if not getattr(fake, "external_id", None):
        setattr(fake, "external_id", item_id_from_url(str(payload.get("url") or "")))

    crm_sent_at = datetime.now().astimezone().isoformat(timespec="seconds")
    listing = build_market_listing_payload(
        fake,
        original_description=payload.get("original_description") if isinstance(payload.get("original_description"), str) else None,
        saved_payload=payload,
        crm_sent_at=crm_sent_at,
    )
    import_payload = build_market_import_payload(
        [listing],
        filename="monitor_live",
        crm_sent_at=crm_sent_at,
        source=str(payload.get("source") or "avito_playwright_parser"),
    )
    result = post_market_import(import_payload, timeout_seconds=60.0)
    if not result.get("ok"):
        raise RuntimeError(f"CRM PostgreSQL import failed: {result}")

    response = result.get("response") if isinstance(result.get("response"), dict) else {}
    created = response.get("created_listing_ids") if isinstance(response, dict) else []
    duplicates = response.get("duplicate_listing_ids") if isinstance(response, dict) else []
    inserted = len(created) if isinstance(created, list) else int(response.get("unique_count") or 0)
    skipped = len(duplicates) if isinstance(duplicates, list) else int(response.get("duplicate_count") or 0)
    return ImportStats(inserted=inserted, updated=0, skipped=skipped, metrics=inserted)


def item_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    match = re.search(r"_(\d{6,})(?:[/?#]|$)", url)
    if match:
        return match.group(1)
    match = re.search(r"/(\d{6,})(?:[/?#]|$)", url)
    return match.group(1) if match else None


def clean_text(value: object) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value).replace("\xa0", " ")).strip()
    return text or None


def split_text_block(value: object) -> list[str]:
    if value is None:
        return []
    lines = []
    for line in str(value).replace("\xa0", " ").splitlines():
        cleaned = re.sub(r"\s+", " ", line).strip()
        if cleaned:
            lines.append(cleaned)
    return lines


def text_from_html_fragment(fragment: str) -> str | None:
    text = re.sub(r"<script[\s\S]*?</script>", " ", fragment, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"<[^>]*$", " ", text)
    return clean_text(unescape(text))


def concrete_address(value: object) -> str | None:
    text = clean_text(value)
    if not text:
        return None
    text = re.sub(r"^во всех регионах\s*", "", text, flags=re.IGNORECASE).strip(" ,")
    return text or None


def address_from_html_file(path: str | Path | None) -> str | None:
    if not path:
        return None
    try:
        html_text = Path(path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    match = re.search(
        r'<[^>]+itemprop=["\']address["\'][^>]*>[\s\S]{0,2500}?(?=<ymaps|</section>|</article>|id=["\']item-view-map|data-marker=)',
        html_text,
        flags=re.IGNORECASE,
    )
    if not match:
        match = re.search(r'id=["\']item-view-address["\'][\s\S]{0,3000}', html_text, flags=re.IGNORECASE)
    if not match:
        return None
    text = text_from_html_fragment(match.group(0))
    if not text:
        return None
    text = re.sub(r"\bМестоположение\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bУзнать подробности\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bПоказать на карте\b", " ", text, flags=re.IGNORECASE)
    return clean_text(text)


def listing_field_statuses(record: NewListingRecord) -> dict[str, object]:
    delivery_status = record.delivery_status or delivery_status_from_text(record.delivery_text)
    if record.delivery_price_rub is not None:
        delivery_display = f"от {record.delivery_price_rub} ₽"
    elif delivery_status == "delivery_not_available":
        delivery_display = "нет"
    elif delivery_status == "delivery_available_price_unknown":
        delivery_display = "есть, цена не найдена"
    else:
        delivery_display = "не нашёл"
    return {
        "title": record.title or "не нашёл",
        "price": record.price if record.price is not None else "не нашёл",
        "address": concrete_address(record.address) or "не нашёл",
        "delivery": delivery_display,
        "delivery_status": delivery_status,
        "delivery_price_rub": record.delivery_price_rub,
    }


def parse_price_rub(*values: object) -> int | None:
    for value in values:
        if value is None:
            continue
        text = str(value).replace("\xa0", " ")
        match = re.search(r"(\d[\d\s]*)", text)
        if not match:
            continue
        try:
            amount = int(match.group(1).replace(" ", ""))
        except ValueError:
            continue
        if amount > 0:
            return amount
    return None


def infer_delivery_status(delivery_text: str | None) -> tuple[str, int | None]:
    price = delivery_price_from_text(delivery_text)
    status = delivery_status_from_text(delivery_text)
    return status, price


def infer_item_kind(title: str | None, description: str | None, params_text: str | None = None) -> str:
    text = " ".join(part for part in (title, description, params_text) if part)
    lowered = text.lower().replace("\u0451", "\u0435").replace("\xa0", " ")
    controller_markers = (
        "dualshock",
        "dualsense",
        "controller",
        "\u0433\u0435\u0439\u043c\u043f\u0430\u0434",
        "\u0434\u0436\u043e\u0439\u0441\u0442\u0438\u043a",
    )
    console_markers = (
        "ps4 slim",
        "ps4 pro",
        "ps4 fat",
        "ps 4 slim",
        "ps 4 pro",
        "playstation 4 slim",
        "playstation 4 pro",
        "playstation 4 fat",
        "playstation 4",
        "sony playstation 4",
        "500gb",
        "500 gb",
        "1tb",
        "1 tb",
        "\u043f\u0440\u0438\u0441\u0442\u0430\u0432",
        "\u043a\u043e\u043d\u0441\u043e\u043b",
        "\u043f\u0440\u043e\u0448\u0438\u0432\u043a",
    )
    game_markers = (
        "\u0434\u0438\u0441\u043a",
        "\u0434\u0438\u0441\u043a\u0438",
        "\u0438\u0433\u0440\u0430",
        "\u0438\u0433\u0440\u044b",
        "game",
        "games",
    )
    digital_markers = (
        "\u0430\u043a\u043a\u0430\u0443\u043d\u0442",
        "\u0446\u0438\u0444\u0440",
        "\u043f\u043e\u0434\u043f\u0438\u0441\u043a",
        "\u0430\u0440\u0435\u043d\u0434",
        "\u043f\u0440\u043e\u043a\u0430\u0442",
    )
    if any(marker in lowered for marker in digital_markers):
        return "digital"
    if any(marker in lowered for marker in console_markers):
        return "console"
    if any(marker in lowered for marker in controller_markers):
        return "accessory"
    if any(marker in lowered for marker in game_markers):
        return "game"
    return item_kind_from_title(title)


def decode_escaped_json_string(raw: str) -> Any:
    if raw == "true":
        return True
    if raw == "false":
        return False
    if raw == "null":
        return None
    if re.fullmatch(r"-?\d+(?:\.\d+)?", raw):
        return int(float(raw))
    if raw.startswith(r"\""):
        try:
            return json.loads(raw.replace(r"\"", '"'))
        except json.JSONDecodeError:
            return raw
    return raw


def first_escaped_json_field(html_text: str, field: str) -> Any:
    pattern = re.compile(
        r'\\"' + re.escape(field) + r'\\"\s*:\s*(\\"(?:\\\\.|[^\\"])*\\"|-?\d+(?:\.\d+)?|true|false|null)'
    )
    match = pattern.search(html_text)
    return decode_escaped_json_string(match.group(1)) if match else None


def first_delivery_reserved(html_text: str) -> bool | None:
    match = re.search(
        r'\\"deliveryInfo\\"\s*:\s*\{[^{}]{0,800}?\\"isReserved\\"\s*:\s*(true|false|null)',
        html_text,
    )
    value = decode_escaped_json_string(match.group(1)) if match else None
    return value if isinstance(value, bool) else None


def parse_server_date(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%A, %d-%b-%y %H:%M:%S %Z", "%a, %d-%b-%y %H:%M:%S %Z"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def normalize_sort_formated_date(value: Any, server_date: Any, collected_at: str | None) -> str | None:
    text = str(value or "").strip().lower().replace("\xa0", " ")
    match = re.search(r"^(сегодня|вчера)\s+в\s+(\d{1,2}):(\d{2})$", text)
    if not match:
        return None
    base = parse_server_date(server_date)
    if base is None and collected_at:
        try:
            base = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
        except ValueError:
            base = None
    if base is None:
        return None
    local_base = base.astimezone(timezone(timedelta(hours=3)))
    day = local_base.date()
    if match.group(1) == "вчера":
        day = day - timedelta(days=1)
    posted = datetime(
        day.year,
        day.month,
        day.day,
        int(match.group(2)),
        int(match.group(3)),
        tzinfo=timezone(timedelta(hours=3)),
    )
    return posted.isoformat(timespec="seconds")


def extract_avito_state_from_html_file(path: str | Path | None, collected_at: str | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        html_text = Path(path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}
    sort_date = first_escaped_json_field(html_text, "sortFormatedDate")
    server_date = first_escaped_json_field(html_text, "date")
    is_active = first_escaped_json_field(html_text, "isActive")
    finish_time = first_escaped_json_field(html_text, "finishTime")
    is_reserved = first_delivery_reserved(html_text)
    return {
        "posted_at_text": sort_date,
        "posted_at": normalize_sort_formated_date(sort_date, server_date, collected_at),
        "server_date": server_date,
        "is_reserved": is_reserved,
        "reservation_status": "reserved" if is_reserved is True else ("available" if is_reserved is False else None),
        "is_active": is_active if isinstance(is_active, bool) else None,
        "finish_time": finish_time if isinstance(finish_time, int) else None,
    }


def monitor_json_to_record(data: dict[str, Any]) -> NewListingRecord:
    visible = data.get("visible") if isinstance(data.get("visible"), dict) else {}
    catalog_summary = data.get("catalog_summary") if isinstance(data.get("catalog_summary"), dict) else {}
    snapshot = data.get("snapshot") if isinstance(data.get("snapshot"), dict) else {}
    avito_state = data.get("avito_state") if isinstance(data.get("avito_state"), dict) else {}
    if not avito_state and snapshot.get("html"):
        avito_state = extract_avito_state_from_html_file(snapshot.get("html"), clean_text(data.get("collected_at")))

    url = clean_text(data.get("canonical_url") or data.get("final_url") or data.get("requested_url"))
    external_id = item_id_from_url(url or clean_text(data.get("requested_url")))
    bot_listing_id = clean_text(data.get("bot_listing_id") or data.get("external_id")) or external_id
    title = clean_text(visible.get("title") or catalog_summary.get("name") or data.get("page_title"))
    price = parse_price_rub(catalog_summary.get("price"), visible.get("price"))
    description = clean_text(visible.get("description") or data.get("meta_description"))
    address = concrete_address(visible.get("address"))
    if snapshot.get("html"):
        html_address = address_from_html_file(snapshot.get("html"))
        if html_address:
            address = html_address
    delivery_text = clean_text(visible.get("delivery"))
    delivery_status, delivery_price = infer_delivery_status(delivery_text)
    explicit_delivery_price = parse_price_rub(data.get("delivery_price_rub"))
    if explicit_delivery_price is not None:
        delivery_price = explicit_delivery_price
        delivery_status = "delivery_available_price_found"

    raw_card_texts = [
        value
        for value in (
            title,
            clean_text(visible.get("price")),
            clean_text(catalog_summary.get("name")),
            clean_text(data.get("page_title")),
        )
        if value
    ]
    raw_detail_texts: list[str] = []
    for key in ("description", "parameters", "address", "seller", "delivery"):
        raw_detail_texts.extend(split_text_block(visible.get(key)))
    if data.get("meta_description"):
        raw_detail_texts.append(str(data.get("meta_description")))
    if snapshot.get("html"):
        raw_detail_texts.append(f"html_snapshot: {snapshot.get('html')}")

    fingerprint_parts = [external_id or "", title or "", str(price or ""), url or ""]
    fingerprint = "|".join(part for part in fingerprint_parts if part) or f"monitor-{datetime.now().timestamp()}"
    saved_at = clean_text(data.get("collected_at")) or datetime.now().isoformat(timespec="seconds")

    record = NewListingRecord(
        saved_at=saved_at,
        fingerprint=fingerprint,
        title=title,
        price=price,
        address=address,
        description=description,
        url=url,
        raw_card_texts=raw_card_texts,
        raw_detail_texts=raw_detail_texts,
        delivery_text=delivery_text,
        delivery_price_rub=delivery_price,
        delivery_status=delivery_status,
        seller_city=seller_city_from_address(address),
        item_kind=infer_item_kind(title, description, clean_text(visible.get("parameters"))),
    )
    setattr(record, "source", "avito_playwright_parser")
    setattr(record, "external_id", external_id)
    setattr(record, "bot_listing_id", bot_listing_id)
    image_urls = extract_image_urls_from_details(data)
    if image_urls:
        setattr(record, "image_urls", image_urls)
    for key in ("posted_at", "posted_at_text", "server_date", "is_reserved", "is_active", "finish_time"):
        if avito_state.get(key) is not None:
            setattr(record, key, avito_state.get(key))
    if isinstance(data.get("monitor_context"), dict):
        setattr(record, "monitor_context", data["monitor_context"])
    setattr(record, "listing_field_statuses", listing_field_statuses(record))
    return record


def extract_image_urls_from_details(data: dict[str, Any]) -> list[str]:
    """Collect public image URLs already captured by the browser scraper."""

    candidates: list[Any] = []
    for key in ("image_urls", "images", "photo_urls", "photos"):
        value = data.get(key)
        if isinstance(value, list):
            candidates.extend(value)
        elif isinstance(value, str):
            candidates.append(value)

    catalog = data.get("catalog_summary")
    if isinstance(catalog, dict):
        image = catalog.get("image")
        if isinstance(image, list):
            candidates.extend(image)
        elif isinstance(image, str):
            candidates.append(image)

    for node in data.get("json_ld") or []:
        if not isinstance(node, dict):
            continue
        graph = node.get("@graph") if isinstance(node.get("@graph"), list) else [node]
        for item in graph:
            if not isinstance(item, dict):
                continue
            image = item.get("image")
            if isinstance(image, list):
                candidates.extend(image)
            elif isinstance(image, str):
                candidates.append(image)

    result: list[str] = []
    seen: set[str] = set()
    for value in candidates:
        url = str(value).strip()
        if not url.startswith(("http://", "https://")):
            continue
        if not is_avito_listing_image_url(url):
            continue
        dedupe_key = avito_image_dedupe_key(url)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        result.append(url)
    return result


def is_avito_listing_image_url(url: str) -> bool:
    """Return True only for real Avito listing/gallery images."""
    normalized = str(url or "").strip()
    lowered = normalized.lower()
    if not AVITO_LISTING_IMAGE_RE.match(normalized):
        return False
    return not any(token in lowered for token in IMAGE_BLOCKLIST_TOKENS)


def avito_image_dedupe_key(url: str) -> str:
    """Return a stable key for one real Avito photo across srcset/size variants."""
    match = AVITO_IMAGE_TOKEN_RE.search(str(url or ""))
    if not match:
        return str(url or "").strip()
    token = match.group(1)
    return f"avito-image:{token[:7]}"


def build_saved_payload(
    record: NewListingRecord,
    *,
    source_path: str | Path | None = None,
    original_description: str | None = None,
    telegram_suppression_reason_value: str | None = None,
    reservation_status: str | None = None,
) -> dict[str, Any]:
    payload = {
        "saved_at": record.saved_at,
        "source": "avito_playwright_parser",
        "source_path": str(source_path) if source_path else None,
        "external_id": getattr(record, "external_id", None),
        "bot_listing_id": getattr(record, "bot_listing_id", None) or getattr(record, "external_id", None),
        "fingerprint": record.fingerprint,
        "title": record.title,
        "price": record.price,
        "address": record.address,
        "description": record.description,
        "original_description": original_description,
        "url": record.url,
        "raw_card_texts": record.raw_card_texts,
        "raw_detail_texts": record.raw_detail_texts,
        "delivery_text": record.delivery_text,
        "delivery_price_rub": record.delivery_price_rub,
        "delivery_status": record.delivery_status,
        "listing_field_statuses": getattr(record, "listing_field_statuses", listing_field_statuses(record)),
        "telegram_suppressed": bool(telegram_suppression_reason_value),
        "telegram_suppression_reason": telegram_suppression_reason_value,
        "reservation_status": reservation_status,
        "image_urls": getattr(record, "image_urls", None),
        "posted_at": getattr(record, "posted_at", None),
        "posted_at_text": getattr(record, "posted_at_text", None),
        "avito_server_date": getattr(record, "server_date", None),
        "is_reserved": getattr(record, "is_reserved", None),
        "is_active": getattr(record, "is_active", None),
        "finish_time": getattr(record, "finish_time", None),
        "monitor_context": getattr(record, "monitor_context", None),
        "llm_extracted_items": getattr(record, "llm_extracted_items", None),
        "llm_route": getattr(record, "llm_route", None),
        "llm_model": getattr(record, "llm_model", None),
        "llm_response_model": getattr(record, "llm_response_model", None),
        "llm_timeout_seconds": getattr(record, "llm_timeout_seconds", None),
        "llm_price_lines_count": getattr(record, "llm_price_lines_count", None),
        "llm_price_line_chunks": getattr(record, "llm_price_line_chunks", None),
        "llm_error": getattr(record, "llm_error", None),
    }
    return payload


def process_details(
    details: dict[str, Any],
    *,
    source_path: str | Path | None = None,
    send_telegram: bool = True,
    send_crm: bool = True,
    analysis_enabled: bool = True,
) -> dict[str, Any]:
    settings = load_automation_settings(SETTINGS_PATH)
    record = monitor_json_to_record(details)
    original_description = record.description

    reservation_status = (
        "reserved"
        if getattr(record, "is_reserved", None) is True
        else listing_reservation_status(record)
    )
    setattr(record, "reservation_status", reservation_status)

    suppression_reason = telegram_suppression_reason(record)
    telegram_result = None
    profit_telegram_result = None
    if send_telegram and not suppression_reason:
        full_description = record.description
        if analysis_enabled:
            record.description = summarize_description_if_needed(
                record.description,
                settings,
                threshold=description_summary_threshold(settings),
            )
        else:
            record.description = truncate_description_for_telegram(
                record.description,
                threshold=description_summary_threshold(settings),
            )
        telegram_result = send_listing_plain_if_configured(record, settings)
        record.description = full_description

    llm_items = (
        extract_listing_items_if_configured(
            record,
            settings,
            original_description=original_description,
        )
        if analysis_enabled
        else None
    )
    if llm_items is not None:
        setattr(record, "llm_extracted_items", llm_items)

    record.description = truncate_description_for_telegram(
        record.description,
        threshold=description_summary_threshold(settings),
    )

    payload = build_link_monitor_payload(
        record,
        original_description=original_description,
        telegram_suppression_reason=suppression_reason,
        reservation_status=reservation_status,
    )
    payload["source"] = "avito_playwright_parser"
    payload["source_path"] = str(source_path) if source_path else None

    saved_payload = build_saved_payload(
        record,
        source_path=source_path,
        original_description=original_description,
        telegram_suppression_reason_value=suppression_reason,
        reservation_status=reservation_status,
    )
    db_result = save_record_to_db(saved_payload, source_path=source_path)
    if send_crm:
        crm_result = {"ok": True, "stored_by": "save_record_to_db", "database": "postgresql"}
        if analysis_enabled:
            crm_evaluation_result = evaluate_record_profit_if_configured(
                record,
                original_description=original_description,
                saved_payload=saved_payload,
            )
            response = crm_evaluation_result.get("response")
            if crm_evaluation_result.get("ok") and isinstance(response, dict):
                response["external_id"] = getattr(record, "external_id", None)
                response["bot_listing_id"] = getattr(record, "bot_listing_id", None) or getattr(record, "external_id", None)
                saved_payload["crm_evaluation_result"] = response
                saved_payload["crm_evaluation_saved_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                save_record_to_db(saved_payload, source_path=source_path)
        else:
            crm_evaluation_result = {"ok": True, "skipped": True, "reason": "analysis_disabled"}
    else:
        crm_result = {"ok": True, "skipped": True, "reason": "disabled"}
        crm_evaluation_result = {"ok": True, "skipped": True, "reason": "disabled"}
    payload["crm_import_result"] = compact_external_result(crm_result)
    payload["crm_evaluation_result"] = compact_external_result(crm_evaluation_result)

    if record.url:
        append_url_text(record.url)

    if send_telegram and not suppression_reason:
        if crm_evaluation_result.get("ok") and not crm_evaluation_result.get("skipped"):
            response = crm_evaluation_result.get("response")
            if isinstance(response, dict):
                profit_telegram_result = send_profit_evaluation_if_configured(
                    response,
                    settings,
                    reply_to=telegram_result,
                )
    payload["telegram_result"] = compact_external_result(telegram_result or {"ok": False, "skipped": True})
    payload["profit_telegram_result"] = compact_external_result(profit_telegram_result or {"ok": False, "skipped": True})
    write_link_monitor_payload(payload)

    return {
        "ok": True,
        "title": record.title,
        "url": record.url,
        "telegram_suppression_reason": suppression_reason,
        "analysis_enabled": analysis_enabled,
        "llm_items_count": len(getattr(record, "llm_extracted_items", []) or []),
        "db_result": {
            "inserted": db_result.inserted,
            "updated": db_result.updated,
            "skipped": db_result.skipped,
            "metrics": db_result.metrics,
        },
        "crm_import_result": compact_external_result(crm_result),
        "crm_evaluation_result": compact_external_result(crm_evaluation_result),
        "telegram_result": telegram_result,
        "profit_telegram_result": profit_telegram_result,
    }


def process_file(
    path: str | Path,
    *,
    send_telegram: bool = False,
    send_crm: bool = True,
    analysis_enabled: bool = True,
) -> dict[str, Any]:
    source_path = Path(path)
    details = json.loads(source_path.read_text(encoding="utf-8"))
    if isinstance(details, dict) and isinstance(details.get("items"), list):
        results = [
            process_details(
                item,
                source_path=source_path,
                send_telegram=send_telegram,
                send_crm=send_crm,
                analysis_enabled=analysis_enabled,
            )
            for item in details["items"]
            if isinstance(item, dict)
        ]
        return {"ok": all(bool(item.get("ok")) for item in results), "count": len(results), "items": results}
    return process_details(
        details,
        source_path=source_path,
        send_telegram=send_telegram,
        send_crm=send_crm,
        analysis_enabled=analysis_enabled,
    )


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run old Avito analysis/CRM logic for Monitor parser JSON.")
    parser.add_argument("paths", nargs="+", help="JSON files produced by qa_automation.py")
    parser.add_argument("--telegram", action="store_true", help="Send Telegram messages too")
    parser.add_argument("--no-crm", action="store_true", help="Do not send CRM requests")
    parser.add_argument("--no-analysis", action="store_true", help="Skip LLM and CRM profit evaluation")
    args = parser.parse_args()

    for raw_path in args.paths:
        result = process_file(
            raw_path,
            send_telegram=args.telegram,
            send_crm=not args.no_crm,
            analysis_enabled=not args.no_analysis,
        )
        print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
