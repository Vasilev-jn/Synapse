from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from .crm_market import lot_cost_observations_from_items, market_price_observations_from_items
from .llm_analyzer import interpret_listing_price_for_record
from .monitor import max_cards_per_cycle


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
PARSED_DIR = DATA_DIR / "parsed"
LINK_MONITOR_JSONL_PATH = PARSED_DIR / "link_monitor.jsonl"
LINK_MONITOR_LATEST_PATH = PARSED_DIR / "link_monitor_latest.json"
LINKS_TXT_PATH = PARSED_DIR / "links.txt"


def telegram_suppression_reason(record: object) -> str | None:
    text = normalized_listing_text(record)
    if has_rental_marker(text):
        return "rental_or_prokat"
    if has_subscription_marker(text) and not has_hardware_listing_marker(text):
        return "subscription"
    if has_digital_marker(text):
        return "digital_or_account"
    return None


def listing_reservation_status(record: object) -> str | None:
    text = normalized_listing_text(record)
    if has_reservation_marker(text):
        return "reserved"
    return None


def normalized_listing_text(record: object) -> str:
    parts: list[str] = []
    for attr in ("title", "description", "address", "delivery_text"):
        value = getattr(record, attr, None)
        if value:
            parts.append(str(value))
    for attr in ("raw_card_texts", "raw_detail_texts"):
        values = getattr(record, attr, None) or []
        parts.extend(str(item) for item in values if item)
    text = "\n".join(parts).lower().replace("ё", "е").replace("\xa0", " ")
    text = re.sub(r"[^0-9a-zа-я+]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def has_rental_marker(text: str) -> bool:
    return bool(re.search(r"\b(аренд\w*|прокат\w*|на\s+сутки|посуточн\w*)\b", text))


def has_subscription_marker(text: str) -> bool:
    patterns = (
        r"\bподписк\w*\b",
        r"\bps\s*\+?\s*plus\b",
        r"\bpsplus\b",
        r"\bplaystation\s+plus\b",
        r"\bplus\s+(?:essential|extra|deluxe|premium)\b",
        r"\bea\s+play\b",
        r"\bgame\s+pass\b",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def has_hardware_listing_marker(text: str) -> bool:
    patterns = (
        r"\bps4\s*(?:slim|fat|pro)\b",
        r"\bplaystation\s*4\s*(?:slim|fat|pro)\b",
        r"\bps\s*4\s*(?:slim|fat|pro)\b",
        r"\b(?:500|1000)\s*(?:gb|гб)\b",
        r"\b1\s*(?:tb|тб)\b",
        r"\bприставк\w*\b",
        r"\bконсол\w*\b",
        r"\bгеймпад\w*\b",
        r"\bджойстик\w*\b",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def has_digital_marker(text: str) -> bool:
    phrases = (
        "цифровой",
        "цифровая версия",
        "цифровая игра",
        "электронная версия",
        "аккаунт",
        "общий аккаунт",
        "игры на аккаунте",
        "библиотека игр",
        "без диска",
        "код активации",
        "ключ активации",
        "offline активация",
        "оффлайн активация",
        "primary",
        "secondary",
    )
    if any(f" {phrase} " in f" {text} " for phrase in phrases):
        return True
    return bool(re.search(r"\b(?:p|п)\s*[123]\b", text))


def has_reservation_marker(text: str) -> bool:
    patterns = (
        r"\bзабронирован\w*\b",
        r"\bзабронировано\s+на\b",
        r"\bзарезервирован\w*\b",
        r"\bтовар\s+зарезервирован\b",
        r"\bв\s+брон[ьи]\b",
        r"\bбронь\b",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def link_seen_key(fingerprint: str | None, url: str) -> str:
    return f"{fingerprint or '<no-fingerprint>'}|{url}"


def description_summary_threshold(settings: dict[str, object]) -> int:
    pipeline = settings.get("pipeline", {})
    if not isinstance(pipeline, dict):
        return 500
    return int(pipeline.get("description_summary_threshold_chars", 500))


def truncate_description_for_telegram(description: str | None, *, threshold: int = 500) -> str | None:
    if not description:
        return description
    cleaned = re.sub(r"\s+", " ", description).strip()
    if len(cleaned) <= threshold:
        return cleaned
    return cleaned[:threshold].rstrip() + "..."


def build_link_monitor_payload(
    record: object,
    *,
    original_description: str | None = None,
    telegram_suppression_reason: str | None = None,
    reservation_status: str | None = None,
) -> dict[str, object]:
    return {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "fingerprint": getattr(record, "fingerprint", None),
        "seen_key": link_seen_key(getattr(record, "fingerprint", None), getattr(record, "url", "") or ""),
        "title": getattr(record, "title", None),
        "price": getattr(record, "price", None),
        "address": getattr(record, "address", None),
        "description": getattr(record, "description", None),
        "original_description": original_description,
        "url": getattr(record, "url", None),
        "raw_card_texts": getattr(record, "raw_card_texts", []),
        "raw_detail_texts": getattr(record, "raw_detail_texts", []),
        "delivery_text": getattr(record, "delivery_text", None),
        "delivery_price_rub": getattr(record, "delivery_price_rub", None),
        "delivery_status": getattr(record, "delivery_status", None),
        "listing_field_statuses": getattr(record, "listing_field_statuses", None),
        "telegram_suppressed": bool(telegram_suppression_reason),
        "telegram_suppression_reason": telegram_suppression_reason,
        "reservation_status": reservation_status,
        "image_urls": getattr(record, "image_urls", None),
        "llm_extracted_items": getattr(record, "llm_extracted_items", None),
        "llm_route": getattr(record, "llm_route", None),
        "llm_model": getattr(record, "llm_model", None),
        "llm_response_model": getattr(record, "llm_response_model", None),
        "llm_timeout_seconds": getattr(record, "llm_timeout_seconds", None),
        "llm_price_lines_count": getattr(record, "llm_price_lines_count", None),
        "llm_price_line_chunks": getattr(record, "llm_price_line_chunks", None),
        "llm_error": getattr(record, "llm_error", None),
        "llm_listing_price_interpretation": interpret_listing_price_for_record(record),
        "llm_market_price_observations": market_price_observations_from_items(
            getattr(record, "llm_extracted_items", None)
        ),
        "llm_lot_cost_observations": lot_cost_observations_from_items(
            getattr(record, "llm_extracted_items", None)
        ),
    }


def compact_external_result(result: dict[str, object]) -> dict[str, object]:
    compact: dict[str, object] = {
        "ok": bool(result.get("ok")),
        "skipped": bool(result.get("skipped")),
    }
    for key in ("status", "reason", "error"):
        value = result.get(key)
        if value is not None:
            compact[key] = value
    response = result.get("response")
    if isinstance(response, dict):
        compact["response"] = response
    chats = result.get("chats")
    if isinstance(chats, list):
        compact["chats"] = chats
    for key in ("sent_as", "photo_send_error", "fallback_text"):
        value = result.get(key)
        if value is not None:
            compact[key] = value
    return compact


def write_link_monitor_payload(payload: dict[str, object]) -> dict[str, object]:
    LINK_MONITOR_JSONL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LINK_MONITOR_JSONL_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    latest: list[dict[str, object]] = []
    if LINK_MONITOR_LATEST_PATH.exists():
        try:
            loaded = json.loads(LINK_MONITOR_LATEST_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                latest = loaded
        except json.JSONDecodeError:
            latest = []
    latest.append(payload)
    LINK_MONITOR_LATEST_PATH.write_text(json.dumps(latest[-200:], ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def append_link_monitor_item(
    record: object,
    *,
    original_description: str | None = None,
    telegram_suppression_reason: str | None = None,
    reservation_status: str | None = None,
) -> dict[str, object]:
    payload = build_link_monitor_payload(
        record,
        original_description=original_description,
        telegram_suppression_reason=telegram_suppression_reason,
        reservation_status=reservation_status,
    )
    return write_link_monitor_payload(payload)


def append_url_text(url: str) -> None:
    LINKS_TXT_PATH.parent.mkdir(parents=True, exist_ok=True)
    known = set()
    if LINKS_TXT_PATH.exists():
        known = {line.strip() for line in LINKS_TXT_PATH.read_text(encoding="utf-8", errors="replace").splitlines()}
    if url in known:
        return
    with LINKS_TXT_PATH.open("a", encoding="utf-8") as handle:
        handle.write(url + "\n")


def monitor_cycle_wait_seconds(settings: dict[str, object]) -> float:
    pipeline = settings.get("pipeline", {})
    if not isinstance(pipeline, dict):
        return 60.0
    return float(pipeline.get("monitor_cycle_wait_seconds", pipeline.get("link_monitor_cycle_wait_seconds", 60.0)))


def link_monitor_scan_rows(settings: dict[str, object]) -> int:
    pipeline = settings.get("pipeline", {})
    if not isinstance(pipeline, dict):
        return 2
    return max(1, int(pipeline.get("link_monitor_scan_rows", 2)))


def link_monitor_row_limit(settings: dict[str, object], override: int | None) -> int:
    if override is not None:
        return max(1, int(override))
    return max(1, max_cards_per_cycle(settings))
