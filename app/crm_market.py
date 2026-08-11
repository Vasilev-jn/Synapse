from __future__ import annotations

import json
import logging
import http.client
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .env import get_env
from .llm_analyzer import interpret_listing_price_for_record

DEFAULT_CRM_MARKET_API_URL = "http://127.0.0.1:8001/api/market/imports/avito"
DEFAULT_CRM_MARKET_EVALUATE_API_URL = "http://127.0.0.1:8001/api/market/evaluate-avito-listing"
STALE_CRM_HOSTS = {"192.168.0.48", "192.168.0.49"}
ALIAS_REVIEW_QUEUE_PATH = Path("json_responses") / "alias_review_queue.jsonl"

GENERIC_MATCH_TOKENS = {
    "ps",
    "ps4",
    "ps5",
    "playstation",
    "sony",
    "game",
    "games",
    "disc",
    "disk",
    "for",
    "on",
    "new",
    "edition",
    "collection",
    "the",
    "of",
    "and",
    "игра",
    "игры",
    "диск",
    "диски",
    "для",
    "на",
    "сони",
    "плейстейшен",
    "коллекция",
    "часть",
    "лот",
    "лотом",
    "комплект",
    "набор",
    "новый",
    "новая",
}


def avito_external_id(url: str | None) -> str | None:
    if not url:
        return None
    match = re.search(r"_(\d{6,})(?:[/?#]|$)", url)
    if match:
        return match.group(1)
    match = re.search(r"/(\d{6,})(?:[/?#]|$)", url)
    return match.group(1) if match else None


def listing_type_from_record(record: object) -> str | None:
    item_kind = str(getattr(record, "item_kind", "") or "").strip()
    if item_kind == "console":
        return "console_bundle"
    if item_kind == "digital":
        return "digital_account"
    if item_kind == "accessory":
        return "accessory"
    if item_kind == "game":
        return "game"
    return item_kind or None


def listing_format_from_record(record: object) -> str | None:
    item_kind = str(getattr(record, "item_kind", "") or "").strip()
    if item_kind == "digital":
        return "digital"
    if item_kind in {"game", "console", "accessory"}:
        return "physical"
    return None


def build_market_listing_payload(
    record: object,
    *,
    original_description: str | None = None,
    saved_payload: dict[str, Any] | None = None,
    crm_sent_at: str | None = None,
) -> dict[str, Any]:
    url = getattr(record, "url", None)
    external_id = getattr(record, "external_id", None) or avito_external_id(url)
    bot_listing_id = getattr(record, "bot_listing_id", None) or external_id
    bot_saved_at = str(saved_payload.get("saved_at")) if saved_payload and saved_payload.get("saved_at") else None
    raw_json: dict[str, Any] = {
        "source": getattr(record, "source", None) or "adb_bot",
        "fingerprint": getattr(record, "fingerprint", None),
        "bot_saved_at": bot_saved_at,
        "crm_sent_at": crm_sent_at,
        "delivery_text": getattr(record, "delivery_text", None),
        "delivery_price_rub": getattr(record, "delivery_price_rub", None),
        "delivery_status": getattr(record, "delivery_status", None),
        "listing_field_statuses": getattr(record, "listing_field_statuses", None),
        "seller_city": getattr(record, "seller_city", None),
        "reservation_status": getattr(record, "reservation_status", None),
        "posted_at": getattr(record, "posted_at", None),
        "posted_at_text": getattr(record, "posted_at_text", None),
        "avito_server_date": getattr(record, "server_date", None),
        "is_reserved": getattr(record, "is_reserved", None),
        "is_active": getattr(record, "is_active", None),
        "finish_time": getattr(record, "finish_time", None),
        "monitor_context": getattr(record, "monitor_context", None),
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
        "crm_evaluation_result": getattr(record, "crm_evaluation_result", None),
        "crm_evaluation_saved_at": getattr(record, "crm_evaluation_saved_at", None),
        "raw_card_texts": getattr(record, "raw_card_texts", []),
        "raw_detail_texts": getattr(record, "raw_detail_texts", []),
    }
    if saved_payload:
        if raw_json.get("crm_evaluation_result") is None and saved_payload.get("crm_evaluation_result") is not None:
            raw_json["crm_evaluation_result"] = saved_payload.get("crm_evaluation_result")
        if raw_json.get("crm_evaluation_saved_at") is None and saved_payload.get("crm_evaluation_saved_at") is not None:
            raw_json["crm_evaluation_saved_at"] = saved_payload.get("crm_evaluation_saved_at")
    if saved_payload:
        raw_json["link_monitor_payload"] = saved_payload

    return {
        "external_id": external_id,
        "bot_listing_id": bot_listing_id,
        "title": getattr(record, "title", None),
        "description": original_description if original_description is not None else getattr(record, "description", None),
        "url": url,
        "price": getattr(record, "price", None),
        "currency": "RUB",
        "address": getattr(record, "address", None),
        "platform": "PS4",
        "format": listing_format_from_record(record),
        "type": listing_type_from_record(record),
        "localization": None,
        "seller_name": None,
        "seller_user_key": None,
        "seller_type": None,
        "is_shop": None,
        "posted_at": getattr(record, "posted_at", None),
        "scraped_at": bot_saved_at,
        "raw_json": raw_json,
    }


def market_price_observations_from_items(items: object) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    observations: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("use_for_market_pricing") is not True:
            continue
        price = item.get("price_rub")
        if price is None:
            price = item.get("price")
        try:
            price_int = int(price)
        except (TypeError, ValueError):
            continue
        if price_int <= 0:
            continue
        observations.append(
            {
                "name": item.get("name"),
                "canonical_name": item.get("canonical_name"),
                "catalog_item_id": item.get("catalog_item_id"),
                "item_type": item.get("item_type"),
                "platform": item.get("platform"),
                "quantity": item.get("quantity") or 1,
                "price_rub": price_int,
                "price_source_type": item.get("price_source_type"),
                "price_scope": item.get("price_scope"),
                "price_confidence": item.get("price_confidence"),
                "price_source_text": item.get("price_source_text"),
                "price_explanation": item.get("price_explanation"),
            }
        )
    return observations


def lot_cost_observations_from_items(items: object) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    observations: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("use_for_lot_cost_estimate") is not True:
            continue
        unit_price = item.get("lot_unit_buy_price_rub")
        total_price = item.get("lot_total_price_rub")
        try:
            unit_price_int = int(unit_price)
            total_price_int = int(total_price)
        except (TypeError, ValueError):
            continue
        if unit_price_int <= 0 or total_price_int <= 0:
            continue
        observations.append(
            {
                "name": item.get("name"),
                "canonical_name": item.get("canonical_name"),
                "catalog_item_id": item.get("catalog_item_id"),
                "item_type": item.get("item_type"),
                "platform": item.get("platform"),
                "quantity": item.get("quantity") or 1,
                "lot_total_price_rub": total_price_int,
                "lot_unit_buy_price_rub": unit_price_int,
                "lot_unit_buy_price_confidence": item.get("lot_unit_buy_price_confidence"),
                "lot_unit_buy_price_source_type": item.get("lot_unit_buy_price_source_type"),
                "price_source_text": item.get("price_source_text"),
                "price_explanation": item.get("price_explanation"),
            }
        )
    return observations


def build_market_import_payload(
    listings: list[dict[str, Any]],
    *,
    filename: str = "adb_bot_live",
    crm_sent_at: str | None = None,
    source: str = "adb_bot",
) -> dict[str, Any]:
    sent_at = crm_sent_at or datetime.now().astimezone().isoformat(timespec="seconds")
    return {
        "source": source,
        "filename": filename,
        "scraped_at": sent_at,
        "crm_sent_at": sent_at,
        "listings": listings,
    }


def post_market_import(payload: dict[str, Any], *, timeout_seconds: float = 5.0) -> dict[str, Any]:
    url = active_crm_import_url()
    if not url:
        return {"ok": True, "skipped": True, "reason": "CRM_MARKET_API_URL is not set"}

    headers = {"Content-Type": "application/json"}
    token = get_env("CRM_MARKET_API_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if is_local_http_url(url):
        return post_json_localhost(url, body=body, headers=headers, timeout_seconds=timeout_seconds)
    request = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            response_body = response.read().decode("utf-8", errors="replace")
            data = json.loads(response_body) if response_body.strip() else {}
            return {"ok": 200 <= response.status < 300, "status": response.status, "response": data}
    except HTTPError as error:
        response_body = error.read().decode("utf-8", errors="replace")
        logging.warning("crm_market_http_error status=%s body=%s", error.code, response_body[:500])
        return {"ok": False, "status": error.code, "error": response_body}
    except (URLError, TimeoutError, OSError) as error:
        logging.warning("crm_market_request_failed error=%s", error)
        return {"ok": False, "error": str(error)}


def crm_evaluation_url() -> str | None:
    explicit = get_env("CRM_MARKET_EVALUATE_API_URL") or get_env("CRM_EVALUATE_API_URL")
    if explicit and not is_stale_crm_url(explicit):
        return explicit
    import_url = get_env("CRM_MARKET_API_URL")
    if import_url and is_stale_crm_url(import_url):
        import_url = None
    if not import_url:
        return DEFAULT_CRM_MARKET_EVALUATE_API_URL
    marker = "/api/market/imports/avito"
    if marker in import_url:
        return import_url.replace(marker, "/api/market/evaluate-avito-listing")
    return import_url.rstrip("/") + "/evaluate-avito-listing"


def alias_review_record(
    *,
    item: dict[str, Any],
    listing_text: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "queued_at": datetime.now().isoformat(timespec="seconds"),
        "reason": reason,
        "listing_title": first_listing_text_line(listing_text),
        "raw_item_name": item.get("name") or item.get("title"),
        "raw_canonical_name": item.get("canonical_name"),
        "raw_catalog_item_id": item.get("catalog_item_id"),
        "raw_catalog_entry_id": item.get("catalog_entry_id"),
        "raw_item_type": item.get("item_type") or item.get("type"),
    }


def append_alias_review_record(record: dict[str, Any]) -> None:
    try:
        ALIAS_REVIEW_QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with ALIAS_REVIEW_QUEUE_PATH.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError as error:
        logging.warning("alias_review_queue_write_failed error=%s", error)


def normalize_guard_text(value: Any) -> str:
    text = str(value or "").casefold().replace("ё", "е").replace("С‘", "Рµ")
    parts: list[str] = []
    current: list[str] = []
    for char in text:
        if char.isalnum():
            current.append(char)
        elif current:
            parts.append("".join(current))
            current = []
    if current:
        parts.append("".join(current))
    return " ".join(parts)


def distinctive_tokens(value: Any) -> set[str]:
    return {
        token
        for token in normalize_guard_text(value).split()
        if len(token) >= 3 and token not in GENERIC_MATCH_TOKENS and not token.isdigit()
    }


def has_distinctive_overlap(left: Any, right: Any) -> bool:
    left_tokens = distinctive_tokens(left)
    right_tokens = distinctive_tokens(right)
    return bool(left_tokens and right_tokens and left_tokens & right_tokens)


def first_listing_text_line(listing_text: str) -> str:
    for line in str(listing_text or "").splitlines():
        clean = line.strip()
        if clean:
            return clean
    return ""


def listing_has_strong_console_evidence(listing_text: str) -> bool:
    text = normalize_guard_text(listing_text)
    return bool(
        re.search(r"\b(ps4|ps5|playstation 4|playstation 5)\b", text)
        and any(
            marker in text
            for marker in (
                "slim",
                "fat",
                "pro",
                "digital",
                "500",
                "825",
                "1tb",
                "1 тб",
                "1000",
                "cuh",
                "приставка",
                "приставку",
                "консоль",
                "консолью",
            )
        )
    )


def listing_looks_like_game_disc(listing_text: str) -> bool:
    text = normalize_guard_text(listing_text)
    return any(marker in text for marker in ("диск", "диски", "игра", "игры", "disc", "disk", "game")) and bool(
        re.search(r"\b(ps4|ps5|playstation 4|playstation 5)\b", text)
    )


def item_looks_like_console(item: dict[str, Any]) -> bool:
    item_type = str(item.get("item_type") or item.get("type") or "").strip().lower()
    text = normalize_guard_text(
        " ".join(
            str(item.get(key) or "")
            for key in ("name", "canonical_name", "catalog_item_id", "catalog_entry_id")
        )
    )
    return item_type in {"console", "game_console"} or (
        bool(re.search(r"\b(ps4|ps5|playstation 4|playstation 5)\b", text))
        and any(marker in text for marker in ("slim", "fat", "pro", "digital", "500", "825", "1tb", "1000"))
    )


def clear_catalog_match_for_alias_review(item: dict[str, Any], *, listing_text: str, reason: str) -> None:
    append_alias_review_record(alias_review_record(item=item, listing_text=listing_text, reason=reason))
    title = first_listing_text_line(listing_text)
    if title:
        item["name"] = title
    item["canonical_name"] = None
    item["catalog_item_id"] = None
    item["catalog_entry_id"] = None
    item["catalog_match_confidence"] = "none"
    item["missing_data_reason"] = reason
    item["use_for_market_pricing"] = False
    item["use_for_lot_cost_estimate"] = False
    if item_looks_like_console(item) and listing_looks_like_game_disc(listing_text):
        item["item_type"] = "game"
        item["type"] = "game"


def guard_unsafe_llm_match(item: dict[str, Any], *, listing_text: str) -> None:
    if not listing_text:
        return
    item_name = item.get("name") or item.get("title") or ""
    canonical = item.get("canonical_name") or ""
    catalog_id = item.get("catalog_item_id") or item.get("catalog_entry_id")
    if item_looks_like_console(item) and listing_looks_like_game_disc(listing_text) and not listing_has_strong_console_evidence(listing_text):
        clear_catalog_match_for_alias_review(
            item,
            listing_text=listing_text,
            reason="console_match_rejected_for_game_disc_listing",
        )
        return
    if distinctive_tokens(listing_text) and item_name and distinctive_tokens(item_name) and not has_distinctive_overlap(item_name, listing_text):
        clear_catalog_match_for_alias_review(
            item,
            listing_text=listing_text,
            reason="llm_item_name_not_found_in_listing_text",
        )
        return
    if (
        distinctive_tokens(listing_text)
        and canonical
        and catalog_id
        and distinctive_tokens(canonical)
        and not has_distinctive_overlap(canonical, f"{item_name}\n{listing_text}")
    ):
        clear_catalog_match_for_alias_review(
            item,
            listing_text=listing_text,
            reason="catalog_match_not_supported_by_listing_text",
        )


def normalize_extracted_items_for_crm(items: object, *, listing_text: str = "") -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if should_skip_item_for_profit_evaluation(item):
            continue
        copied = dict(item)
        if copied.get("price") is None and copied.get("price_rub") is not None:
            copied["price"] = copied.get("price_rub")
        if copied.get("price_rub") is None and copied.get("price") is not None:
            copied["price_rub"] = copied.get("price")
        fix_obvious_item_type_mistakes(copied)
        guard_unsafe_llm_match(copied, listing_text=listing_text)
        if not copied.get("missing_data_reason"):
            refine_console_catalog_match(copied, listing_text=listing_text)
        normalized.append(copied)
    return dedupe_console_items(normalized)


def dedupe_console_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    console_by_generation: dict[str, dict[str, Any]] = {}
    for item in items:
        generation = console_generation_for_item(item)
        if not generation:
            result.append(item)
            continue
        existing = console_by_generation.get(generation)
        if existing is None:
            console_by_generation[generation] = item
            result.append(item)
            continue
        preferred = preferred_console_item(existing, item)
        if preferred is existing:
            continue
        console_by_generation[generation] = item
        index = result.index(existing)
        result[index] = item
    return result


def console_generation_for_item(item: dict[str, Any]) -> str | None:
    item_type = str(item.get("item_type") or item.get("type") or "").strip().lower()
    text = " ".join(str(item.get(key) or "") for key in ("name", "canonical_name", "catalog_item_id")).lower()
    if item_type not in {"console", "game_console"} and not re.search(r"\b(ps4|ps5|playstation\s*[45])\b", text):
        return None
    if re.search(r"\b(ps5|playstation\s*5)\b", text):
        return "ps5"
    if re.search(r"\b(ps4|playstation\s*4)\b", text):
        return "ps4"
    return None


def preferred_console_item(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    def score(item: dict[str, Any]) -> tuple[int, int, int]:
        text = " ".join(str(item.get(key) or "") for key in ("name", "canonical_name", "catalog_item_id")).lower()
        exact = 1 if item.get("catalog_entry_id") else 0
        specific = sum(1 for marker in ("slim", "pro", "fat", "digital", "500", "1tb", "1 tb", "1000") if marker in text)
        priced = 1 if item.get("price_rub") is not None or item.get("price") is not None else 0
        return (exact, specific, priced)

    return right if score(right) > score(left) else left


def fix_obvious_item_type_mistakes(item: dict[str, Any]) -> None:
    name = str(item.get("name") or item.get("title") or "").lower().replace("ё", "е")
    if "unknown" in name and "game" in name:
        item["item_type"] = "game"
        item["type"] = "game"
        item["canonical_name"] = None
        item["catalog_item_id"] = None
        item["catalog_entry_id"] = None
        item["catalog_match_confidence"] = "none"
        return
    if any(marker in name for marker in ("dualshock", "dualsense", "геймпад", "джойстик", "controller")):
        item["item_type"] = "controller"
        item["type"] = "controller"
        return
    if any(marker in name for marker in ("hdmi", "power cable", "кабель", "провод", "шнур")):
        item["item_type"] = "accessory"
        item["type"] = "accessory"


def refine_console_catalog_match(item: dict[str, Any], *, listing_text: str = "") -> None:
    """Prefer exact CRM console catalog entries over generic LLM ids.

    LLM can reasonably say "PlayStation 4 Slim", but if it also sends the old
    generic catalog_item_id=playstation4, CRM evaluates it as plain PS4. For
    consoles we can safely infer the exact catalog_entry_id from title +
    description because models/storage are explicit text markers.
    """

    item_type = str(item.get("item_type") or item.get("type") or "").strip().lower()
    item_name_text = str(item.get("name") or "").lower().replace("ё", "е").replace("\xa0", " ")
    item_name_text = re.sub(r"\s+", " ", item_name_text)
    item_self_text = " ".join(
        str(value or "")
        for value in (
            item.get("name"),
            item.get("canonical_name"),
            item.get("catalog_item_id"),
        )
    ).lower().replace("ё", "е").replace("\xa0", " ")
    item_self_text = re.sub(r"\s+", " ", item_self_text)
    text = " ".join(
        str(value or "")
        for value in (
            item.get("name"),
            item.get("canonical_name"),
            item.get("catalog_item_id"),
            listing_text,
        )
    ).lower().replace("ё", "е").replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    console_pattern = r"\b(ps4|ps5|playstation\s*[45]|sony\s*playstation\s*[45])\b"
    non_console_name = any(
        marker in item_name_text
        for marker in ("game", "игра", "диск", "dualshock", "dualsense", "геймпад", "джойстик", "кабель", "провод")
    )
    item_names_a_console = bool(
        not non_console_name
        and (
        re.search(console_pattern, item_name_text)
        or any(marker in item_name_text for marker in ("приставка", "консоль"))
        )
    )
    if item_type not in {"console", "game_console"} and not item_names_a_console:
        return

    target: tuple[int, str] | None = None
    if re.search(r"\b(ps5|playstation\s*5)\b", text):
        slim = "slim" in text
        digital = any(marker in text for marker in ("digital", "дигитал", "без дисковода", "digital edition"))
        if slim and digital:
            target = (67, "PlayStation 5 Slim Digital")
        elif slim:
            target = (66, "PlayStation 5 Slim Disc")
        elif digital:
            target = (65, "PlayStation 5 Digital")
        else:
            target = (64, "PlayStation 5 Disc")
    elif re.search(r"\b(ps4|playstation\s*4)\b", text):
        one_tb = bool(
            re.search(r"\b(1\s*tb|1\s*тб|1000\s*gb|1000\s*гб)\b", text)
            or re.search(r"\b(гб|gb)\s*[:：]\s*1000\b", text)
            or re.search(r"\b(память|storage)[^:：]{0,60}[:：]\s*1000\b", text)
        )
        if "pro" in text:
            target = (63, "PlayStation 4 Pro 1TB")
        elif "slim" in text:
            target = (62, "PlayStation 4 Slim 1TB") if one_tb else (61, "PlayStation 4 Slim 500GB")
        elif any(marker in text for marker in ("fat", "phat", "фат")):
            target = (60, "PlayStation 4 Fat 1TB") if one_tb else (59, "PlayStation 4 Fat 500GB")

    if not target:
        return
    catalog_entry_id, canonical_name = target
    item["catalog_entry_id"] = catalog_entry_id
    item["canonical_name"] = canonical_name
    item["catalog_item_id"] = None
    item["catalog_match_confidence"] = "high"
    item["item_type"] = "console"


def should_skip_item_for_profit_evaluation(item: dict[str, Any]) -> bool:
    combined_text = " ".join(
        str(item.get(key) or "")
        for key in (
            "name",
            "canonical_name",
            "item_type",
            "price_source_text",
            "price_explanation",
            "missing_data_reason",
            "buyer_thoughts",
            "notes",
            "extraction_status",
        )
    ).lower().replace("ё", "е").replace("С‘", "Рµ")
    hard_skip_markers = (
        "скупка",
        "выкуп",
        "куплю",
        "оценю",
        "принимаю",
        "обменяю ваш",
        "ремонт",
        "услуга",
        "услуги",
        "создание профиля",
        "создам профиль",
        "пополнение",
        "активация",
        "подписка",
        "аренда",
        "прокат",
        "аккаунт",
        "цифров",
        "digital",
        "key",
        "ключ",
        "прошивка",
        "blocked_by_junk_listing",
    )
    item_type = str(item.get("item_type") or "").strip().lower()
    if item_type in {"digital_account", "subscription", "service"}:
        return True
    if item.get("is_physical") is False:
        return True
    if item_type in {"console", "game_console"}:
        return False
    if any(marker in combined_text for marker in hard_skip_markers):
        return True
    item_type = str(item.get("item_type") or "")
    name = str(item.get("name") or "").lower().replace("ё", "е")
    if item_type == "unknown" and item.get("catalog_item_id"):
        item["catalog_item_id"] = None
        item["canonical_name"] = None
        item["catalog_match_confidence"] = "none"
    low_value_accessory_markers = (
        "hdmi",
        "usb",
        "кабель",
        "шнур",
        "провод",
        "инструкция",
        "документация",
        "коробка",
        "ножки",
        "подставка",
        "футляр",
    )
    valuable_accessory_markers = ("геймпад", "джойстик", "controller", "dualsense", "dualshock", "зарядн")
    if item_type == "accessory" and any(marker in name for marker in low_value_accessory_markers):
        return not any(marker in name for marker in valuable_accessory_markers)
    return False


def build_listing_evaluation_payload(
    record: object,
    *,
    original_description: str | None = None,
    saved_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    listing = build_market_listing_payload(
        record,
        original_description=original_description,
        saved_payload=saved_payload,
    )
    delivery_price = getattr(record, "delivery_price_rub", None)
    listing["delivery_price_rub"] = delivery_price
    listing["delivery_price"] = delivery_price
    listing["delivery_status"] = getattr(record, "delivery_status", None)
    listing["fingerprint"] = getattr(record, "fingerprint", None)
    listing_text = "\n".join(
        str(value or "")
        for value in (
            getattr(record, "title", None),
            original_description if original_description is not None else getattr(record, "description", None),
            "\n".join(getattr(record, "raw_detail_texts", []) or []),
        )
    )
    return {
        "listing": listing,
        "extracted_items": normalize_extracted_items_for_crm(
            getattr(record, "llm_extracted_items", None),
            listing_text=listing_text,
        ),
    }


def post_listing_evaluation(payload: dict[str, Any], *, timeout_seconds: float = 10.0) -> dict[str, Any]:
    url = crm_evaluation_url()
    if not url:
        return {"ok": True, "skipped": True, "reason": "CRM evaluation URL is not set"}

    headers = {"Content-Type": "application/json"}
    token = get_env("CRM_MARKET_API_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if is_local_http_url(url):
        return post_json_localhost(url, body=body, headers=headers, timeout_seconds=timeout_seconds)
    request = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            response_body = response.read().decode("utf-8", errors="replace")
            data = json.loads(response_body) if response_body.strip() else {}
            return {"ok": 200 <= response.status < 300, "status": response.status, "response": data}
    except HTTPError as error:
        response_body = error.read().decode("utf-8", errors="replace")
        logging.warning("crm_evaluation_http_error status=%s body=%s", error.code, response_body[:500])
        return {"ok": False, "status": error.code, "error": response_body}
    except (URLError, TimeoutError, OSError) as error:
        logging.warning("crm_evaluation_request_failed error=%s", error)
        return {"ok": False, "error": str(error)}


def evaluate_record_profit_if_configured(
    record: object,
    *,
    original_description: str | None = None,
    saved_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = build_listing_evaluation_payload(
        record,
        original_description=original_description,
        saved_payload=saved_payload,
    )
    return post_listing_evaluation(payload, timeout_seconds=crm_evaluation_timeout_seconds())


def crm_evaluation_timeout_seconds() -> float:
    raw = get_env("CRM_MARKET_EVALUATE_TIMEOUT_SECONDS") or get_env("CRM_EVALUATE_TIMEOUT_SECONDS")
    if not raw:
        return 4.0


def is_local_http_url(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}


def is_stale_crm_url(url: str | None) -> bool:
    if not url:
        return False
    return (urlsplit(url).hostname or "") in STALE_CRM_HOSTS


def active_crm_import_url() -> str:
    configured = get_env("CRM_MARKET_API_URL")
    if configured and not is_stale_crm_url(configured):
        return configured
    return DEFAULT_CRM_MARKET_API_URL


def post_json_localhost(
    url: str,
    *,
    body: bytes,
    headers: dict[str, str],
    timeout_seconds: float,
) -> dict[str, Any]:
    parsed = urlsplit(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"
    conn = http.client.HTTPConnection(host, port=port, timeout=timeout_seconds)
    try:
        conn.request("POST", path, body=body, headers=headers)
        response = conn.getresponse()
        response_body = response.read().decode("utf-8", errors="replace")
        data = json.loads(response_body) if response_body.strip() else {}
        return {"ok": 200 <= response.status < 300, "status": response.status, "response": data}
    except (TimeoutError, OSError, http.client.HTTPException) as error:
        logging.warning("crm_local_request_failed error=%s", error)
        return {"ok": False, "error": str(error)}
    finally:
        conn.close()
    try:
        return max(1.0, float(raw.replace(",", ".")))
    except ValueError:
        return 4.0


def send_record_to_crm_if_configured(
    record: object,
    *,
    original_description: str | None = None,
    saved_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    crm_sent_at = datetime.now().astimezone().isoformat(timespec="seconds")
    listing = build_market_listing_payload(
        record,
        original_description=original_description,
        saved_payload=saved_payload,
        crm_sent_at=crm_sent_at,
    )
    payload = build_market_import_payload(
        [listing],
        filename=f"{getattr(record, 'source', None) or 'adb_bot'}_live",
        crm_sent_at=crm_sent_at,
        source=getattr(record, "source", None) or "adb_bot",
    )
    return post_market_import(payload)
