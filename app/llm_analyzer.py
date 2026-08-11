from __future__ import annotations

import json
import http.client
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict
from typing import Any

from .catalog_search import catalog_candidates_for_record, catalog_candidates_for_text, catalog_items_by_id, normalize_catalog_text
from .env import get_env
from .monitor import NewListingRecord


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
POLZA_URL = "https://polza.ai/api/v1/chat/completions"
POLZA_FALLBACK_IP = "158.160.170.34"
DEFAULT_MODEL = "qwen/qwen3-30b-a3b-instruct-2507"
DEFAULT_HEAVY_MODEL = "google/gemma-3-27b-it"
DEFAULT_HEAVY_FALLBACK_MODEL = ""
MAX_NORMALIZED_ITEMS = 500
PRICE_LINE_MIN_PRICE = 100
PRICE_LINE_MAX_PRICE = 100_000
BAD_CATALOG_ITEM_IDS = {"e_obek_ayk", "metal_gear_solid"}
GENERIC_CATALOG_ITEM_IDS = {"playstation_4", "playstation_5"}
HEAVY_RAIN_BEYOND_ID = "heavy_rain_and_beyond_two_souls"
HEAVY_RAIN_BEYOND_NAME = "Heavy Rain And Beyond Two Souls"


def llm_enabled(settings: dict[str, object]) -> bool:
    integrations = settings.get("integrations", {})
    if not isinstance(integrations, dict):
        return False
    llm = integrations.get("llm", {})
    if not isinstance(llm, dict):
        return False
    return bool(llm.get("enabled", False))


def llm_model(settings: dict[str, object]) -> str:
    integrations = settings.get("integrations", {})
    if isinstance(integrations, dict):
        llm = integrations.get("llm", {})
        if isinstance(llm, dict) and llm.get("model"):
            return str(llm["model"])
    return DEFAULT_MODEL


def llm_heavy_model(settings: dict[str, object]) -> str:
    integrations = settings.get("integrations", {})
    if isinstance(integrations, dict):
        llm = integrations.get("llm", {})
        if isinstance(llm, dict) and llm.get("heavy_model"):
            return str(llm["heavy_model"])
    return DEFAULT_HEAVY_MODEL


def llm_heavy_fallback_model(settings: dict[str, object]) -> str:
    integrations = settings.get("integrations", {})
    if isinstance(integrations, dict):
        llm = integrations.get("llm", {})
        if isinstance(llm, dict) and llm.get("heavy_fallback_model"):
            return str(llm["heavy_fallback_model"])
    return DEFAULT_HEAVY_FALLBACK_MODEL


def llm_provider(settings: dict[str, object]) -> str:
    integrations = settings.get("integrations", {})
    if isinstance(integrations, dict):
        llm = integrations.get("llm", {})
        if isinstance(llm, dict) and llm.get("provider"):
            return str(llm["provider"]).strip().lower()
    return "openrouter"


def llm_api_url(provider: str) -> str:
    return POLZA_URL if provider == "polza" else OPENROUTER_URL


def llm_api_key(provider: str) -> str | None:
    if provider == "polza":
        return get_env("POLZA_API_KEY")
    return get_env("OPENROUTER_API_KEY")


def llm_missing_key_message(provider: str) -> str:
    env_name = "POLZA_API_KEY" if provider == "polza" else "OPENROUTER_API_KEY"
    return f"{env_name} is not set"


def llm_items_max_tokens(settings: dict[str, object]) -> int:
    raw = get_env("OPENROUTER_ITEMS_MAX_TOKENS")
    if raw:
        try:
            return max(512, int(raw))
        except ValueError:
            pass
    integrations = settings.get("integrations", {})
    if isinstance(integrations, dict):
        llm = integrations.get("llm", {})
        if isinstance(llm, dict) and llm.get("items_max_tokens"):
            try:
                return max(512, int(llm["items_max_tokens"]))
            except (TypeError, ValueError):
                pass
    return 4096


def llm_heavy_items_max_tokens(settings: dict[str, object]) -> int:
    raw = get_env("POLZA_HEAVY_ITEMS_MAX_TOKENS") or get_env("OPENROUTER_HEAVY_ITEMS_MAX_TOKENS")
    if raw:
        try:
            return max(1024, int(raw))
        except ValueError:
            pass
    integrations = settings.get("integrations", {})
    if isinstance(integrations, dict):
        llm = integrations.get("llm", {})
        if isinstance(llm, dict) and llm.get("heavy_items_max_tokens"):
            try:
                return max(1024, int(llm["heavy_items_max_tokens"]))
            except (TypeError, ValueError):
                pass
    return 8192


def llm_items_timeout_seconds(settings: dict[str, object]) -> int:
    raw = get_env("POLZA_ITEMS_TIMEOUT_SECONDS") or get_env("OPENROUTER_ITEMS_TIMEOUT_SECONDS")
    if raw:
        try:
            return max(10, int(raw))
        except ValueError:
            pass
    integrations = settings.get("integrations", {})
    if isinstance(integrations, dict):
        llm = integrations.get("llm", {})
        if isinstance(llm, dict) and llm.get("items_timeout_seconds"):
            try:
                return max(10, int(llm["items_timeout_seconds"]))
            except (TypeError, ValueError):
                pass
    return 60


def llm_heavy_items_timeout_seconds(settings: dict[str, object]) -> int:
    raw = get_env("POLZA_HEAVY_ITEMS_TIMEOUT_SECONDS") or get_env("OPENROUTER_HEAVY_ITEMS_TIMEOUT_SECONDS")
    if raw:
        try:
            return max(30, int(raw))
        except ValueError:
            pass
    integrations = settings.get("integrations", {})
    if isinstance(integrations, dict):
        llm = integrations.get("llm", {})
        if isinstance(llm, dict) and llm.get("heavy_items_timeout_seconds"):
            try:
                return max(30, int(llm["heavy_items_timeout_seconds"]))
            except (TypeError, ValueError):
                pass
    return 240


def llm_heavy_routing_enabled(settings: dict[str, object]) -> bool:
    integrations = settings.get("integrations", {})
    if not isinstance(integrations, dict):
        return True
    llm = integrations.get("llm", {})
    if not isinstance(llm, dict):
        return True
    return bool(llm.get("heavy_routing_enabled", True))


def llm_price_line_chunking_enabled(settings: dict[str, object]) -> bool:
    llm = llm_settings(settings)
    return bool(llm.get("price_line_chunking_enabled", True))


def llm_price_line_min_count(settings: dict[str, object]) -> int:
    return llm_int_setting(settings, "price_line_min_count", default=12, minimum=3)


def llm_price_line_chunk_size(settings: dict[str, object]) -> int:
    return llm_int_setting(settings, "price_line_chunk_size", default=30, minimum=5)


def llm_price_line_max_chunks(settings: dict[str, object]) -> int:
    return llm_int_setting(settings, "price_line_max_chunks", default=6, minimum=1)


def llm_settings(settings: dict[str, object]) -> dict[str, object]:
    integrations = settings.get("integrations", {})
    if not isinstance(integrations, dict):
        return {}
    llm = integrations.get("llm", {})
    return llm if isinstance(llm, dict) else {}


def llm_int_setting(settings: dict[str, object], key: str, *, default: int, minimum: int) -> int:
    value = llm_settings(settings).get(key)
    try:
        return max(minimum, int(value)) if value is not None else default
    except (TypeError, ValueError):
        return default


def analyze_listing_if_configured(record: NewListingRecord, settings: dict[str, object]) -> dict[str, object] | None:
    if not llm_enabled(settings):
        return ensure_title_console_items(record, [], original_description=original_description)
    provider = llm_provider(settings)
    model = llm_model(settings)
    api_key = llm_api_key(provider)
    if not api_key:
        return llm_error(model, llm_missing_key_message(provider), "llm_not_configured")
    return analyze_listing(record, api_key=api_key, model=model, api_url=llm_api_url(provider))


def summarize_description_if_needed(description: str | None, settings: dict[str, object], *, threshold: int = 500) -> str | None:
    if not description:
        return description
    cleaned = re.sub(r"\s+", " ", description).strip()
    if len(cleaned) <= threshold:
        return description.strip()
    if not llm_enabled(settings):
        return cleaned[:threshold].rstrip() + "..."
    provider = llm_provider(settings)
    model = llm_model(settings)
    api_key = llm_api_key(provider)
    if not api_key:
        return cleaned[:threshold].rstrip() + "..."
    summary = summarize_description(cleaned, api_key=api_key, model=model, api_url=llm_api_url(provider))
    return summary or (cleaned[:threshold].rstrip() + "...")


def summarize_description(
    description: str,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    api_url: str = OPENROUTER_URL,
) -> str | None:
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Коротко пересказывай описание объявления Авито для покупателя. "
                    "Без markdown, без JSON, без выдумывания. "
                    "Сначала определи реальный товар из названия/первой строки: игра, диск, консоль, аккаунт, аксессуар или лот. "
                    "Важно: строка 'Платформа: PlayStation 4/PS4/PS5' означает платформу игры, а не то, что продаётся консоль. "
                    "Не называй товар консолью, если в тексте нет явных слов 'консоль', 'приставка', 'PlayStation 4 500GB/1TB', 'PS4 Slim/Fat/Pro' как предмет продажи. "
                    "Для игры/диска укажи название игры, платформу, физический это диск или неясно, состояние, цену/наличие/доставку. "
                    "Для консоли укажи модель, память, комплект, состояние, прошивку и дефекты. "
                    "Рекламный текст магазина выкидывай."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Сожми до 2-4 коротких предложений, максимум 500 символов. "
                    "Не меняй тип товара. Если это игра, начни с 'Игра ...'. Если это консоль, начни с 'Консоль ...'.\n\n"
                    f"{description}"
                ),
            },
        ],
        "temperature": 0.1,
        "max_tokens": 220,
    }
    request = urllib.request.Request(
        api_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost/monitor",
            "X-Title": "Monitor",
        },
        method="POST",
    )
    response_result = openrouter_response_body(request, model)
    if isinstance(response_result, dict):
        return None
    try:
        data = json.loads(response_result)
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(content, str):
        return None
    summary = re.sub(r"\s+", " ", content).strip()
    return summary[:500].strip() or None


def extract_listing_items_if_configured(
    record: NewListingRecord,
    settings: dict[str, object],
    *,
    original_description: str | None = None,
) -> list[dict[str, object]] | None:
    if not llm_enabled(settings):
        return None
    provider = llm_provider(settings)
    api_key = llm_api_key(provider)
    if not api_key:
        return ensure_title_console_items(record, [], original_description=original_description)
    api_url = llm_api_url(provider)
    light_model = llm_model(settings)
    if llm_price_line_chunking_enabled(settings):
        price_lines = extract_price_list_lines_from_record(record, original_description=original_description)
        if len(price_lines) >= llm_price_line_min_count(settings):
            model = llm_heavy_model(settings)
            timeout_seconds = llm_heavy_items_timeout_seconds(settings)
            setattr(record, "llm_route", "price_line_chunks")
            setattr(record, "llm_model", model)
            setattr(record, "llm_timeout_seconds", timeout_seconds)
            setattr(record, "llm_price_lines_count", len(price_lines))
            chunk_result = extract_price_line_items_with_chunks(
                record,
                price_lines,
                api_key=api_key,
                api_url=api_url,
                model=model,
                chunk_size=llm_price_line_chunk_size(settings),
                max_chunks=llm_price_line_max_chunks(settings),
                request_timeout_seconds=timeout_seconds,
            )
            setattr(record, "llm_price_line_chunks", chunk_result.get("chunks"))
            if chunk_result.get("items"):
                setattr(record, "llm_response_model", chunk_result.get("response_model"))
                setattr(record, "llm_error", chunk_result.get("error"))
                return ensure_title_console_items(record, chunk_result["items"], original_description=original_description)
            setattr(record, "llm_error", chunk_result.get("error"))

    heavy_needed = llm_heavy_routing_enabled(settings) and should_use_heavy_llm(record, original_description=original_description)
    route = "heavy" if heavy_needed else "light"
    model = llm_heavy_model(settings) if heavy_needed else light_model
    max_tokens = llm_heavy_items_max_tokens(settings) if heavy_needed else llm_items_max_tokens(settings)
    timeout_seconds = llm_heavy_items_timeout_seconds(settings) if heavy_needed else llm_items_timeout_seconds(settings)

    setattr(record, "llm_route", route)
    setattr(record, "llm_model", model)
    setattr(record, "llm_timeout_seconds", timeout_seconds)
    result = extract_listing_items_with_metadata(
        record,
        api_key=api_key,
        model=model,
        api_url=api_url,
        original_description=original_description,
        max_tokens=max_tokens,
        request_timeout_seconds=timeout_seconds,
    )
    if result.get("ok"):
        setattr(record, "llm_response_model", result.get("response_model"))
        setattr(record, "llm_error", None)
        return ensure_title_console_items(record, result["items"], original_description=original_description)

    if heavy_needed:
        fallback_model = llm_heavy_fallback_model(settings)
        if not fallback_model or fallback_model == model:
            setattr(record, "llm_error", result.get("error"))
            return ensure_title_console_items(record, [], original_description=original_description)
        setattr(record, "llm_route", "heavy_fallback")
        setattr(record, "llm_model", fallback_model)
        fallback_result = extract_listing_items_with_metadata(
            record,
            api_key=api_key,
            model=fallback_model,
            api_url=api_url,
            original_description=original_description,
            max_tokens=max_tokens,
            request_timeout_seconds=timeout_seconds,
        )
        if fallback_result.get("ok"):
            setattr(record, "llm_response_model", fallback_result.get("response_model"))
            setattr(record, "llm_error", result.get("error"))
            return ensure_title_console_items(record, fallback_result["items"], original_description=original_description)
        setattr(record, "llm_error", fallback_result.get("error") or result.get("error"))
        return ensure_title_console_items(record, [], original_description=original_description)

    setattr(record, "llm_error", result.get("error"))
    return ensure_title_console_items(record, [], original_description=original_description)


def ensure_title_console_items(
    record: NewListingRecord,
    items: object,
    *,
    original_description: str | None = None,
) -> list[dict[str, object]]:
    """Add an obvious console from the listing title when LLM misses the main product."""

    normalized_items = [dict(item) for item in items if isinstance(item, dict)] if isinstance(items, list) else []
    console = console_item_from_listing_title(record, original_description=original_description)
    if console is None:
        return normalized_items
    for item in normalized_items:
        if item_matches_console(item, console):
            enrich_console_item_from_title(item, console)
            setattr(record, "llm_title_console_backfilled", True)
            return normalized_items
    if any(item_matches_console(item, console) for item in normalized_items):
        return normalized_items
    setattr(record, "llm_title_console_backfilled", True)
    return [console, *normalized_items]


def console_item_from_listing_title(
    record: NewListingRecord,
    *,
    original_description: str | None = None,
) -> dict[str, object] | None:
    title = str(getattr(record, "title", "") or "")
    description = str(original_description or getattr(record, "description", "") or "")
    title_text = normalize_console_text(title)
    full_text = normalize_console_text(f"{title} {description}")
    price = getattr(record, "price", None)

    if not title_has_console_product_evidence(title_text, full_text):
        return None

    target: tuple[int, str, str] | None = None
    if re.search(r"\b(ps5|playstation\s*5)\b", title_text):
        slim = "slim" in full_text
        digital = any(marker in full_text for marker in ("digital", "digital edition", "диджитал", "без дисковода"))
        if slim and digital:
            target = (67, "PlayStation 5 Slim Digital", "PS5")
        elif slim:
            target = (66, "PlayStation 5 Slim Disc", "PS5")
        elif digital:
            target = (65, "PlayStation 5 Digital", "PS5")
        else:
            target = (64, "PlayStation 5 Disc", "PS5")
    elif re.search(r"\b(ps4|playstation\s*4)\b", title_text):
        one_tb = bool(re.search(r"\b(1\s*tb|1\s*тб|1000\s*gb|1000\s*гб)\b", full_text))
        if "pro" in full_text:
            target = (63, "PlayStation 4 Pro 1TB", "PS4")
        elif "slim" in full_text:
            target = (62, "PlayStation 4 Slim 1TB", "PS4") if one_tb else (61, "PlayStation 4 Slim 500GB", "PS4")
        elif any(marker in full_text for marker in ("fat", "phat", "фат")):
            target = (60, "PlayStation 4 Fat 1TB", "PS4") if one_tb else (59, "PlayStation 4 Fat 500GB", "PS4")
        else:
            target = (60, "PlayStation 4 Fat 1TB", "PS4") if one_tb else (59, "PlayStation 4 Fat 500GB", "PS4")

    if target is None:
        return None
    catalog_entry_id, canonical_name, platform = target
    return {
        "name": canonical_name,
        "canonical_name": canonical_name,
        "catalog_item_id": None,
        "catalog_entry_id": catalog_entry_id,
        "catalog_match_confidence": "high",
        "item_type": "console",
        "platform": platform,
        "quantity": 1,
        "condition": "unknown",
        "is_physical": True,
        "price_rub": None,
        "price_source_type": "title_backfill_total_lot",
        "price_scope": "total_lot",
        "price_confidence": "none",
        "use_for_market_pricing": False,
        "lot_total_price_rub": price,
        "lot_unit_buy_price_rub": None,
        "lot_unit_buy_price_confidence": "none",
        "lot_unit_buy_price_source_type": "unknown",
        "use_for_lot_cost_estimate": False,
        "price_source_text": title,
        "price_explanation": "Console was explicitly named in the listing title; listing price is treated as bundle/lot total, not per-item market price.",
        "price_visibility": "total_lot_price",
        "extraction_status": "title_backfilled",
        "missing_data_reason": "",
        "buyer_thoughts": "Main console was explicit in title; verify bundle contents and condition manually.",
        "notes": "Added by deterministic title safeguard because LLM omitted the main console.",
    }


def normalize_console_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.lower().replace("ё", "е").replace("\xa0", " ")).strip()


def title_has_console_product_evidence(title_text: str, full_text: str) -> bool:
    """Return True only when PS4/PS5 in title appears to name a console.

    Plain "ps4/ps5" near a game title usually means platform, e.g.
    "disc for ps4 far cry 4". Those listings must not get a synthetic console
    item, otherwise CRM calculates absurd profit from a game-only card.
    """

    title_text = normalize_console_text(title_text)
    full_text = normalize_console_text(full_text)
    if not re.search(r"\b(ps4|ps5|playstation\s*[45]|sony\s*playstation\s*[45])\b", title_text):
        return False

    strong_console_markers = (
        "console",
        "consol",
        "пристав",
        "консол",
        "slim",
        "pro",
        "fat",
        "phat",
        "digital",
        "digital edition",
        "cuh",
        "825gb",
        "500gb",
        "1000gb",
        "1tb",
        "500 gb",
        "1000 gb",
        "1 tb",
        "500 гб",
        "1000 гб",
        "1 тб",
        "дисковод",
        "без дисковода",
        "с дисководом",
    )
    if any(marker in full_text for marker in strong_console_markers):
        return True

    game_or_disc_markers = (
        "disc",
        "disk",
        "game",
        "игра",
        "игры",
        "диск",
        "диски",
        "edition",
        "far cry",
        "diablo",
        "gran turismo",
        "baldur",
        "gta",
        "fifa",
        "nba",
    )
    if any(marker in full_text for marker in game_or_disc_markers):
        return False

    return bool(re.search(r"\bsony\s*playstation\s*[45]\b|\bplaystation\s*[45]\b", title_text))


def item_matches_console(item: dict[str, object], console: dict[str, object]) -> bool:
    if item.get("catalog_entry_id") == console.get("catalog_entry_id"):
        return True
    item_type = str(item.get("item_type") or item.get("type") or "").lower()
    text = normalize_console_text(
        " ".join(str(item.get(key) or "") for key in ("name", "canonical_name", "catalog_item_id"))
    )
    if item_type not in {"console", "game_console"} and not re.search(r"\b(ps4|ps5|playstation\s*[45])\b", text):
        return False
    target_text = normalize_console_text(str(console.get("canonical_name") or ""))
    if console_generation(text) and console_generation(text) == console_generation(target_text):
        return True
    return bool(target_text and (target_text in text or text in target_text))


def enrich_console_item_from_title(item: dict[str, object], console: dict[str, object]) -> None:
    for key in (
        "canonical_name",
        "catalog_item_id",
        "catalog_entry_id",
        "catalog_match_confidence",
        "item_type",
        "platform",
        "is_physical",
    ):
        item[key] = console.get(key)
    item.setdefault("quantity", 1)
    item.setdefault("price_rub", None)
    item.setdefault("use_for_market_pricing", False)
    item.setdefault("price_scope", "total_lot")
    item.setdefault("price_source_type", "title_backfill_total_lot")
    item.setdefault("extraction_status", "title_backfilled")


def console_generation(text: str) -> str | None:
    normalized = normalize_console_text(text)
    if re.search(r"\b(ps5|playstation\s*5)\b", normalized):
        return "ps5"
    if re.search(r"\b(ps4|playstation\s*4)\b", normalized):
        return "ps4"
    return None


def extract_listing_items(
    record: NewListingRecord,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    api_url: str = OPENROUTER_URL,
    original_description: str | None = None,
    max_tokens: int = 4096,
    request_timeout_seconds: int = 60,
) -> list[dict[str, object]]:
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You extract sellable products from one Avito listing. "
                    "Return ONLY one valid JSON object shaped as {\"items\": [...]}. "
                    "No markdown, no comments, no intro text, no trailing explanation, no verdict, no profit math. "
                    "The first character must be { and the last character must be }. "
                    "If uncertain, use null/unknown fields inside JSON, never prose outside JSON."
                ),
            },
            {"role": "user", "content": build_items_prompt(record, original_description=original_description)},
        ],
        "temperature": 0.0,
        "max_tokens": max(512, int(max_tokens)),
        "response_format": {"type": "json_object"},
    }
    response_result = openrouter_completion_body(
        payload,
        api_key=api_key,
        model=model,
        api_url=api_url,
        request_timeout_seconds=request_timeout_seconds,
    )
    if isinstance(response_result, dict):
        return []
    try:
        data = json.loads(response_result)
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, json.JSONDecodeError, TypeError):
        return []
    if not isinstance(content, str) or not content.strip():
        return []
    raw_items, parsed_items_key = parse_json_items_with_status(content)
    items = normalize_listing_items(raw_items)
    if not items and not parsed_items_key:
        items = fallback_listing_items_from_record(record)
    items = complete_listing_items_from_source(record, items)
    return sanitize_listing_items_for_record(record, items)


def extract_listing_items_with_metadata(
    record: NewListingRecord,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    api_url: str = OPENROUTER_URL,
    original_description: str | None = None,
    max_tokens: int = 4096,
    request_timeout_seconds: int = 60,
) -> dict[str, object]:
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You extract sellable products from one Avito listing. "
                    "Return ONLY one valid JSON object shaped as {\"items\": [...]}. "
                    "No markdown, no comments, no intro text, no trailing explanation, no verdict, no profit math. "
                    "The first character must be { and the last character must be }. "
                    "If uncertain, use null/unknown fields inside JSON, never prose outside JSON."
                ),
            },
            {"role": "user", "content": build_items_prompt(record, original_description=original_description)},
        ],
        "temperature": 0.0,
        "max_tokens": max(512, int(max_tokens)),
        "response_format": {"type": "json_object"},
    }
    started_at = time.monotonic()
    response_result = openrouter_completion_body(
        payload,
        api_key=api_key,
        model=model,
        api_url=api_url,
        request_timeout_seconds=request_timeout_seconds,
    )
    elapsed = round(time.monotonic() - started_at, 3)
    if isinstance(response_result, dict):
        error = dict(response_result)
        error["requested_model"] = model
        error["elapsed_seconds"] = elapsed
        error["request_timeout_seconds"] = request_timeout_seconds
        return {"ok": False, "items": [], "error": error, "elapsed_seconds": elapsed}
    try:
        data = json.loads(response_result)
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, json.JSONDecodeError, TypeError) as error:
        return {
            "ok": False,
            "items": [],
            "error": llm_error(model, f"Bad LLM response: {error}", "llm_bad_response"),
            "elapsed_seconds": elapsed,
            "request_timeout_seconds": request_timeout_seconds,
        }
    if not isinstance(content, str) or not content.strip():
        return {
            "ok": False,
            "items": [],
            "error": llm_error(model, "Bad LLM response: empty message content", "llm_empty_response"),
            "elapsed_seconds": elapsed,
            "request_timeout_seconds": request_timeout_seconds,
        }
    raw_items, parsed_items_key = parse_json_items_with_status(content)
    items = normalize_listing_items(raw_items)
    if not items and not parsed_items_key:
        items = fallback_listing_items_from_record(record)
    items = complete_listing_items_from_source(record, items)
    items = sanitize_listing_items_for_record(record, items)
    return {
        "ok": True,
        "items": items,
        "requested_model": model,
        "response_model": data.get("model"),
        "elapsed_seconds": elapsed,
        "request_timeout_seconds": request_timeout_seconds,
    }


def should_use_heavy_llm(record: NewListingRecord, *, original_description: str | None = None) -> bool:
    description = original_description if original_description is not None else getattr(record, "description", None)
    raw_card_texts = getattr(record, "raw_card_texts", []) or []
    raw_detail_texts = getattr(record, "raw_detail_texts", []) or []
    text = "\n".join(
        str(part or "")
        for part in [
            getattr(record, "title", None),
            description,
            *raw_card_texts,
            *raw_detail_texts,
        ]
    )
    normalized = text.lower().replace("ё", "е")
    price_context_count = max(len(extract_price_contexts([text], limit=200)), count_visible_price_lines(text))
    long_text = len(text) >= 3500 or len(str(description or "")) >= 2500
    many_detail_rows = len(raw_detail_texts) >= 45
    many_prices = price_context_count >= 6
    hard_markers = (
        "много игр",
        "список",
        "прайс",
        "цены",
        "цена на фото",
        "цены на фото",
        "в личке",
        "в лс",
        "пишите",
        "в наличии",
        "комплект",
        "лот",
        "many games",
        "price list",
        "bundle",
    )
    has_hard_marker = any(marker in normalized for marker in hard_markers)
    return bool((many_prices and has_hard_marker) or long_text or (many_detail_rows and has_hard_marker))


def count_visible_price_lines(text: str) -> int:
    count = 0
    for raw_line in re.split(r"[\n\r;]+", text):
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        if re.search(r"\b\d[\d\s]{1,6}\s*(?:₽|руб\.?|р\.?|rub)\b", line, re.IGNORECASE):
            count += 1
            continue
        if re.search(r"[-:—]\s*\d[\d\s]{1,6}\s*(?:₽|руб\.?|р\.?|rub)?\s*$", line, re.IGNORECASE):
            count += 1
    return count


def extract_price_list_lines_from_record(
    record: NewListingRecord,
    *,
    original_description: str | None = None,
) -> list[dict[str, object]]:
    description = original_description if original_description is not None else getattr(record, "description", None)
    text = "\n".join(
        str(part or "")
        for part in [
            getattr(record, "title", None),
            description,
            *list(getattr(record, "raw_card_texts", []) or []),
            *list(getattr(record, "raw_detail_texts", []) or []),
        ]
    )
    rows: list[dict[str, object]] = []
    previous_without_price = ""
    seen: set[tuple[str, int]] = set()
    for line_no, raw_line in enumerate(re.split(r"[\r\n]+", text), start=1):
        line = clean_price_line(raw_line)
        if not line:
            continue
        if price_line_is_unavailable(line):
            previous_without_price = ""
            continue
        parsed = parse_price_list_line(line)
        if parsed is None and previous_without_price:
            parsed = parse_price_list_line(f"{previous_without_price} {line}")
        if parsed is None:
            previous_without_price = line if likely_price_list_name_fragment(line) else ""
            continue
        previous_without_price = ""
        name, price = parsed
        key = (normalize_price_line_key(name), price)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"line_no": line_no, "name": name, "price_rub": price, "source_line": line})
    return rows


def parse_price_list_line(line: str) -> tuple[str, int] | None:
    patterns = (
        r"^(?P<name>.+?)[\s_\-–—:]+(?P<price>\d[\d\s]{2,5})\s*(?:₽|руб\.?|р\.?|rub)?\.?$",
        r"^(?P<name>.+?)\s+(?P<price>\d{3,5})\s*(?:₽|руб\.?|р\.?|rub)?\.?$",
    )
    for pattern in patterns:
        match = re.match(pattern, line, flags=re.IGNORECASE)
        if not match:
            continue
        name = clean_price_line_name(match.group("name"))
        price = safe_price_line_int(match.group("price"))
        if not name or price is None or price < PRICE_LINE_MIN_PRICE or price > PRICE_LINE_MAX_PRICE:
            continue
        if price_line_name_is_not_product(name):
            continue
        return name, price
    return None


def extract_price_line_items_with_chunks(
    record: NewListingRecord,
    price_lines: list[dict[str, object]],
    *,
    api_key: str,
    api_url: str,
    model: str,
    chunk_size: int,
    max_chunks: int,
    request_timeout_seconds: int,
) -> dict[str, object]:
    all_items: list[dict[str, object]] = []
    chunks: list[dict[str, object]] = []
    response_model: str | None = None
    first_error: object | None = None
    for chunk_index, start in enumerate(range(0, len(price_lines), chunk_size), start=1):
        if chunk_index > max_chunks:
            break
        chunk = price_lines[start : start + chunk_size]
        result = normalize_price_line_chunk(
            chunk,
            api_key=api_key,
            api_url=api_url,
            model=model,
            request_timeout_seconds=request_timeout_seconds,
        )
        chunks.append(
            {
                "chunk_index": chunk_index,
                "input_count": len(chunk),
                "ok": bool(result.get("ok")),
                "items_count": len(result.get("items") or []),
                "elapsed_seconds": result.get("elapsed_seconds"),
                "error": result.get("error"),
            }
        )
        if result.get("response_model"):
            response_model = str(result.get("response_model"))
        if result.get("ok"):
            all_items.extend(price_line_chunk_items_to_listing_items(result.get("items"), chunk))
        elif first_error is None:
            first_error = result.get("error")

    items = normalize_listing_items(all_items)
    items = complete_listing_items_from_source(record, items)
    items = sanitize_listing_items_for_record(record, items)
    if len(price_lines) > chunk_size * max_chunks:
        chunks.append(
            {
                "chunk_index": "truncated",
                "input_count": len(price_lines) - chunk_size * max_chunks,
                "ok": False,
                "items_count": 0,
                "error": "price_line_max_chunks_reached",
            }
        )
    return {
        "items": items,
        "chunks": chunks,
        "response_model": response_model,
        "error": first_error,
    }


def normalize_price_line_chunk(
    chunk: list[dict[str, object]],
    *,
    api_key: str,
    api_url: str,
    model: str,
    request_timeout_seconds: int,
) -> dict[str, object]:
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Normalize Avito price-list rows for PS4/PS5 resale. "
                    "Return only JSON {\"items\": [...]}. Return one item per input row. "
                    "Do not drop rows. Do not invent prices. Keep original_name if unsure."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": "Normalize these already extracted product-price rows.",
                        "schema": {
                            "items": [
                                {
                                    "line_no": 1,
                                    "original_name": "raw product name",
                                    "normalized_name": "clean readable title",
                                    "price_rub": 1000,
                                    "platform": "PS4 | PS5 | unknown",
                                    "item_type": "game | accessory | console | unknown",
                                    "confidence": "high | medium | low",
                                }
                            ]
                        },
                        "rows": chunk,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        "temperature": 0,
        "max_tokens": 6000,
        "response_format": {"type": "json_object"},
    }
    started_at = time.monotonic()
    body = openrouter_completion_body(
        payload,
        api_key=api_key,
        model=model,
        api_url=api_url,
        request_timeout_seconds=request_timeout_seconds,
    )
    elapsed = round(time.monotonic() - started_at, 3)
    if isinstance(body, dict):
        return {"ok": False, "items": [], "elapsed_seconds": elapsed, "error": body}
    try:
        data = json.loads(body)
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        items = parsed.get("items") if isinstance(parsed, dict) else None
    except (KeyError, IndexError, json.JSONDecodeError, TypeError) as error:
        return {"ok": False, "items": [], "elapsed_seconds": elapsed, "error": str(error)}
    return {
        "ok": isinstance(items, list),
        "items": items if isinstance(items, list) else [],
        "response_model": data.get("model"),
        "elapsed_seconds": elapsed,
    }


def price_line_chunk_items_to_listing_items(
    normalized_items: object,
    source_chunk: list[dict[str, object]],
) -> list[dict[str, object]]:
    if not isinstance(normalized_items, list):
        return []
    source_by_line = {int(row.get("line_no") or 0): row for row in source_chunk}
    result: list[dict[str, object]] = []
    for item in normalized_items:
        if not isinstance(item, dict):
            continue
        line_no = safe_int(item.get("line_no"), 0)
        source = source_by_line.get(line_no, {})
        name = str(item.get("normalized_name") or item.get("name") or item.get("original_name") or source.get("name") or "").strip()
        price = safe_int(item.get("price_rub"), 0) or safe_int(source.get("price_rub"), 0)
        if not name or price <= 0:
            continue
        confidence = str(item.get("confidence") or "medium").strip().lower()
        if confidence not in {"high", "medium", "low"}:
            confidence = "medium"
        item_type = str(item.get("item_type") or "game").strip().lower()
        if item_type not in {"game", "accessory", "console", "controller", "unknown"}:
            item_type = "game"
        result.append(
            {
                "name": name,
                "canonical_name": None,
                "catalog_item_id": None,
                "catalog_match_confidence": "none",
                "item_type": item_type,
                "platform": normalize_price_line_platform(item.get("platform")),
                "quantity": 1,
                "condition": "unknown",
                "is_physical": True,
                "price_rub": price,
                "price_source_type": "description_item_price",
                "price_scope": "per_item",
                "price_confidence": confidence,
                "use_for_market_pricing": item_type in {"game", "accessory", "console", "controller"},
                "lot_total_price_rub": None,
                "lot_unit_buy_price_rub": None,
                "lot_unit_buy_price_confidence": "none",
                "lot_unit_buy_price_source_type": "unknown",
                "use_for_lot_cost_estimate": False,
                "price_source_text": str(source.get("source_line") or item.get("original_name") or "")[:240],
                "price_explanation": "Individual price line from seller price list.",
                "price_visibility": "explicit_item_price",
                "extraction_status": "complete",
                "missing_data_reason": "",
                "buyer_thoughts": "Price-list item; verify disc availability and condition before buying.",
                "notes": "price_line_chunk",
            }
        )
    return result


def normalize_price_line_platform(value: object) -> str | None:
    text = str(value or "").upper()
    if "PS5" in text and "PS4" in text:
        return "PS4 | PS5"
    if "PS5" in text:
        return "PS5"
    if "PS4" in text:
        return "PS4"
    return None


def clean_price_line(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip(" .")


def clean_price_line_name(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" -–—_:.,")


def safe_price_line_int(value: object) -> int | None:
    try:
        return int(re.sub(r"\D+", "", str(value)))
    except ValueError:
        return None


def price_line_is_unavailable(line: str) -> bool:
    lowered = line.lower().replace("ё", "е")
    return any(marker in lowered for marker in ("нету", "нет в наличии", "продано"))


def likely_price_list_name_fragment(line: str) -> bool:
    if len(line) < 3 or len(line) > 80:
        return False
    if re.search(r"\d{3,5}", line):
        return False
    lowered = line.lower()
    return not any(marker in lowered for marker in ("цена", "доставка", "самовывоз", "авито"))


def price_line_name_is_not_product(name: str) -> bool:
    lowered = name.lower().replace("ё", "е")
    bad_markers = ("цена", "доставка", "самовывоз", "отправ", "торг", "скидк")
    return any(marker in lowered for marker in bad_markers)


def normalize_price_line_key(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold().replace("ё", "е")).strip()


def build_items_prompt(record: NewListingRecord, *, original_description: str | None = None) -> str:
    description = original_description if original_description is not None else getattr(record, "description", None)
    card = {
        "title": getattr(record, "title", None),
        "listing_price_rub": getattr(record, "price", None),
        "item_kind_hint": getattr(record, "item_kind", None),
        "address": getattr(record, "address", None),
        "seller_city": getattr(record, "seller_city", None),
        "description": description,
        "url": getattr(record, "url", None),
        "delivery_text": getattr(record, "delivery_text", None),
        "delivery_price_rub": getattr(record, "delivery_price_rub", None),
        "delivery_status": getattr(record, "delivery_status", None),
        "reservation_status": getattr(record, "reservation_status", None),
        "raw_card_texts": getattr(record, "raw_card_texts", []),
        "raw_detail_texts": getattr(record, "raw_detail_texts", []),
        "price_contexts": extract_price_contexts(
            [
                getattr(record, "title", None),
                description,
                " ".join(getattr(record, "raw_card_texts", []) or []),
                " ".join(getattr(record, "raw_detail_texts", []) or []),
            ]
        ),
    }
    catalog_candidates = catalog_candidates_for_record(record, limit=50)
    item_schema = {
            "name": "exact product name from the card",
            "canonical_name": "catalog canonical_name if matched, otherwise null",
            "catalog_item_id": "catalog_item_id if matched, otherwise null",
            "catalog_match_confidence": "high | medium | low | none",
            "item_type": "game | console | controller | accessory | subscription | digital_account | service | unknown",
            "platform": "PS4 | PS5 | PS3 | Xbox | Switch | PC | null",
            "quantity": 1,
            "condition": "new | excellent | good | used | broken | unknown",
            "is_physical": True,
            "price_rub": None,
            "price_source_type": "listing_price_single_item | description_item_price | description_total_lot_price | description_each_price | seller_says_price_varies | unknown",
            "price_scope": "per_item | total_lot | each_item_same_price | bundle_total | unknown",
            "price_confidence": "high | medium | low | none",
            "use_for_market_pricing": False,
            "lot_total_price_rub": None,
            "lot_unit_buy_price_rub": None,
            "lot_unit_buy_price_confidence": "high | medium | low | none",
            "lot_unit_buy_price_source_type": "listing_lot_total | description_total_lot_price | pickup_discount_total | explicit_discount_total | unknown",
            "use_for_lot_cost_estimate": False,
            "price_source_text": "exact short text fragment where the price came from, or empty string",
            "price_explanation": "short reason why this price belongs or does not belong to this item",
            "price_visibility": "explicit_item_price | listing_single_item_price | total_lot_price | price_in_photo | price_in_private_messages | price_varies | no_price_visible | unclear",
            "extraction_status": "complete | partial | unclear | photo_only | blocked_by_missing_text",
            "missing_data_reason": "empty if complete; otherwise explain what is missing and where the seller says it is",
            "buyer_thoughts": "short buyer-style assessment: what I would check before buying/reselling",
            "notes": "short factual note, empty string if nothing extra",
        }
    schema = {"items": [item_schema]}
    one_shot_input = {
        "title": "Игры PS4 EA Sports FC 24 и UFC 4",
        "listing_price_rub": 955,
        "description": (
            "Продаю два диска PS4. Цены: EA Sports FC 24 — 1000 рублей; "
            "UFC 4 — 1590 рублей. Если возьмете обе сразу — сделаю скидку."
        ),
    }
    one_shot_output = {
        "items": [
            {
                **item_schema,
                "name": "EA Sports FC 24",
                "canonical_name": None,
                "catalog_item_id": None,
                "catalog_match_confidence": "none",
                "item_type": "game",
                "platform": "PS4",
                "quantity": 1,
                "condition": "unknown",
                "is_physical": True,
                "price_rub": 1000,
                "price_source_type": "description_item_price",
                "price_scope": "per_item",
                "price_confidence": "high",
                "use_for_market_pricing": True,
                "price_source_text": "EA Sports FC 24 — 1000 рублей",
                "price_explanation": "Individual description line price for this exact game; listing price is not used.",
                "notes": "",
            },
            {
                **item_schema,
                "name": "UFC 4",
                "canonical_name": None,
                "catalog_item_id": None,
                "catalog_match_confidence": "none",
                "item_type": "game",
                "platform": "PS4",
                "quantity": 1,
                "condition": "unknown",
                "is_physical": True,
                "price_rub": 1590,
                "price_source_type": "description_item_price",
                "price_scope": "per_item",
                "price_confidence": "high",
                "use_for_market_pricing": True,
                "price_source_text": "UFC 4 — 1590 рублей",
                "price_explanation": "Individual description line price for this exact game; listing price is not used.",
                "notes": "",
            },
        ]
    }
    return (
        "Extract every sellable product/item included in this listing.\n"
        "Rules:\n"
        "- Return only one JSON object shaped like the schema below: {\"items\": [...]}.\n"
        "- Do not write any text before or after JSON. No markdown fences.\n"
        "- Include games, consoles, controllers and real accessories.\n"
        "- The listing title is product evidence. If the title names a console/game/accessory, include it even if the description mostly talks about condition, bundle or reason for sale.\n"
        "- Never ignore the main product named in title. Example: title 'Sony playstation 5 slim digital edition 825gb' must include PlayStation 5 Slim Digital console; controllers/accessories are additional items, not replacements for the console.\n"
        "- HARD JUNK FILTER: if the listing is primarily about buying items from people, buyout, appraisal, repair/service, account/profile creation, PSN wallet top-up, activation, firmware service, subscriptions, rental, digital keys/accounts, boosting, installation, or any non-physical service, do not treat it as a resale candidate.\n"
        "- For junk/service/buying/digital listings, return either an empty items list or only service/digital/subscription items with is_physical=false, use_for_market_pricing=false, use_for_lot_cost_estimate=false, extraction_status=blocked_by_junk_listing, and buyer_thoughts explaining why it is not a physical resale lot.\n"
        "- Russian junk markers include: скупка, выкуп, куплю, оценю, принимаю, обменяю ваш, ремонт, услуга, услуги, создание профиля, создам профиль, пополнение, активация, подписка, аренда, прокат, аккаунт, цифровая версия, цифровой товар, ключ, прошивка как услуга.\n"
        "- Think like an experienced reseller/buyer, not like a blind extractor: identify what is actually being sold, whether the price is usable, what is missing, and what would make you hesitate before buying.\n"
        "- If prices/products are said to be 'in photos', 'on photo', 'in private messages/DM/PM', 'ask in messages', 'prices vary', 'see photo', or similar, do not invent missing products/prices. Set price_visibility accordingly and explain missing_data_reason.\n"
        "- If the text hints that important data is outside readable text (photos, hidden part, private chat), still return visible products, but mark extraction_status=partial/photo_only/unclear and write why.\n"
        "- buyer_thoughts should be short and practical: e.g. 'Need photo check: names/prices likely on photos', 'Bundle price unclear; cannot value each game', 'Console bundle, ask firmware/controllers condition'.\n"
        "- For long lots, do not summarize/squash named products. Every named game/accessory/console line is a separate item. If there are 20 named games, return 20 game items. Use Unknown remainder only for explicitly mentioned unnamed leftovers.\n"
        "- In long lots with price lines, count visible product-price lines before answering; the number of returned concrete items should match those visible lines unless you explain missing_data_reason.\n"
        "- Product/price scenarios:\n"
        "  1) Single physical game: if exactly one named physical disc is sold, listing_price_rub belongs to that game.\n"
        "  2) Game lot with item prices: lines like 'GTA 5 - 1300', 'UFC 4: 1500' are per-item prices; ignore listing_price_rub for those items.\n"
        "  3) Game lot with same price for each item: phrases like 'each disc 900' or 'каждая по 1000' apply only if the items are named or an unnamed Unknown games item is needed.\n"
        "  4) Game lot with total price: phrases like 'for all', 'за все/за всё', 'all together', or listing_price_rub for multiple products are total lot prices, never per-item prices.\n"
        "  4b) Lot cost estimate: if a total lot price is known and several physical games are included, also estimate lot_unit_buy_price_rub = total lot price / number of games. This is a buy-cost estimate, not a market sell price. Do this only for game-only lots, not console bundles.\n"
        "  4c) Discounts: if text says 'for all 5000', 'all together 5000', 'pickup/self-pickup 4500', 'discount if take all', use the explicit discounted total when present; if discount is mentioned without a number, keep listing_price_rub as total and explain that discount is unquantified.\n"
        "  5) Unknown photo-only lot: if text says only 'games/discs for PS4', 'several games', 'many discs', but no game names are visible in CARD_JSON, return Unknown PS4 games and note 'products may be on photos or not listed in text'. Do not invent game names.\n"
        "  6) Console bundle: extract console model/storage, controllers, named games and accessories separately; bundle/listing price must not be copied into every component.\n"
        "  6a) PS5 console model identity: distinguish PlayStation 5 Disc, PlayStation 5 Digital, PlayStation 5 Slim Disc, and PlayStation 5 Slim Digital. 'Digital', 'Digital Edition', 'без дисковода', 'диджитал' means Digital. 'Slim/слим' means Slim. 'с дисководом', visible/mentioned disc drive, or no digital marker means Disc. Do not collapse these into generic PlayStation 5 when the text gives enough evidence.\n"
        "  6b) Console with account/subscription: if a physical console is sold and the text says an account, games on account, PS Plus/Extra/Deluxe subscription, or subscription until a date is included on that console, keep the console as the main physical item and add a separate subscription/digital_account note item with is_physical=false, use_for_market_pricing=false, use_for_lot_cost_estimate=false. Treat it as a small bundle attractiveness bonus/risk, not as a separately resellable product.\n"
        "  6c) Example: 'PS4 Slim 500GB, 1 DualShock, Horizon Zero Dawn, wires, stand, Deluxe subscription until 24 May 2027' means items: PS4 Slim 500GB console, DualShock 4 controller, Horizon Zero Dawn physical game if it is a disc, stand accessory, and PlayStation Plus Deluxe subscription as non-physical bundle note. buyer_thoughts should say subscription adds roughly 300-500 rub value but account transfer/rules must be checked.\n"
        "  7) Digital/subscription/rental/account without a physical console being sold: mark as digital_account/subscription/service, is_physical=false, and do not use as a physical-disc market price.\n"
        "  8) DLC included in an edition: do not split DLC/add-ons into separate sellable items when they are part of one edition, e.g. Witcher 3 GOTY includes Blood and Wine/Hearts of Stone.\n"
        "- If game names are listed, add every named game as a separate item; never replace a list of named games with one generic bundle item.\n"
        "- If a title names several numbered games, split them into separate game items. Example: 'Little Nightmares 1&2' means Little Nightmares and Little Nightmares 2.\n"
        "- If the listing includes a bonus card/map/postcard, include it as an accessory.\n"
        "- Do not collapse named games into 'Unknown games'. Use an unknown games item only for the unnamed remainder.\n"
        "- Example: if the card says '30 games: GTA 5, UFC 4', return GTA 5, UFC 4, and Unknown games quantity 28.\n"
        "- Treat 'Heavy Rain And Beyond Two Souls' / 'Heavy Rain & Beyond: Two Souls' / 'Heavy Rain и За гранью: Две души' as one physical collection/SKU, not as two separate games. If it is the only product, listing_price_rub is its market price.\n"
        "- Use CATALOG_CANDIDATES to normalize product names when there is a clear match.\n"
        "- canonical_name must be copied exactly from CATALOG_CANDIDATES, never invented.\n"
        "- catalog_item_id must be copied exactly from CATALOG_CANDIDATES, never invented.\n"
        "- If no candidate clearly matches the product, set canonical_name and catalog_item_id to null and catalog_match_confidence to none.\n"
        "- Prefer no catalog match over a wrong catalog match.\n"
        "- Never match across different numbered parts/years/editions. FC 24 is not FC 25; FIFA 24 is not FIFA 25; Battlefront 2 is not Battlefront 1; GTA Trilogy is not Mafia Trilogy; Witcher 3 is not another Witcher entry.\n"
        "- If product text contains a number/year and the candidate has a different important number/year, set canonical_name=null and catalog_item_id=null.\n"
        "- Include digital accounts/subscriptions only if the listing actually sells them or they are bundled with a physical console; they are never physical market price observations.\n"
        "- Do not include delivery, installment/credit text, seller, address, ratings, buttons, discounts or app UI as products.\n"
        "- condition must be unknown unless the card explicitly says the condition for that exact item.\n"
        "- price_rub is the price of that exact item only. Never put total bundle/listing price into an item price.\n"
        "- Use listing_price_rub as price_rub only when the listing clearly sells exactly one product; then price_source_type=listing_price_single_item, price_scope=per_item, use_for_market_pricing=true.\n"
        "- If description has lines like 'Game - 1000', 'Game: 1000', or 'Game 1000 rub', assign that price only to that named game; price_source_type=description_item_price, price_scope=per_item, use_for_market_pricing=true.\n"
        "- If description says 'each game 1000', 'price for each disc', set that price for unnamed items only when it clearly applies to them; price_source_type=description_each_price, price_scope=each_item_same_price.\n"
        "- If description says 'all together 5000', 'for all', 'bundle price', or listing_price_rub is for several products, do NOT put it into every item. Use price_rub=null, price_source_type=description_total_lot_price or unknown, price_scope=total_lot/bundle_total, use_for_market_pricing=false.\n"
        "- If the listing price looks like an attention price, deposit, service price, or unclear store placeholder, do NOT use it as item price.\n"
        "- If the listing includes games/controllers/accessories together with a console, it is a bundle: set price_rub to null for every component unless the text gives item-specific prices.\n"
        "- For lots, component price_rub must be item-specific only when the text gives that exact item price; otherwise null.\n"
        "- Set price_confidence=high only for explicit item-specific prices or a clear one-item listing price. Use medium/low/none otherwise.\n"
        "- use_for_market_pricing=true only when price_rub is a reliable market observation for this exact named sellable item, not a lot/bundle/console/attention price.\n"
        "- Unknown/unnamed games or products from photos cannot be used for market pricing even if the listing says 'each 1000', because CRM cannot attach that price to a concrete product.\n"
        "- A total lot/bundle/listing price is a listing-level price observation, not an item-level market price. Keep it out of item price_rub unless the exact item has its own price.\n"
        "- lot_unit_buy_price_rub is allowed for game-only lots: it means approximate buy cost per game inside this listing, computed from a total lot price divided by known game quantity. It must not set use_for_market_pricing=true.\n"
        "- For console bundles, never divide the whole console bundle listing price across games. Keep lot_unit_buy_price_rub=null unless the text gives a separate total price for the games only.\n"
        "- If lot quantity is unknown, do not compute lot_unit_buy_price_rub; say products may be on photos/not listed.\n"
        "- Digital accounts, rentals, subscriptions and services are not physical market price observations.\n"
        "- price_source_text must quote the shortest source fragment such as 'GTA 5 - 1500' or 'listing price 1200 for one disc'.\n"
        "- price_explanation must explain price logic briefly: 'single physical disc', 'price belongs to whole lot', 'individual line price', etc.\n"
        "- If the card says there are unnamed games/discs, add one item like 'Unknown PS4 games' with quantity when known.\n"
        "- Use only the information in CARD_JSON. Do not guess missing products.\n\n"
        f"SCHEMA_EXAMPLE:\n{json.dumps(schema, ensure_ascii=False, indent=2)}\n\n"
        f"ONE_SHOT_INPUT:\n{json.dumps(one_shot_input, ensure_ascii=False, indent=2)}\n\n"
        f"ONE_SHOT_OUTPUT:\n{json.dumps(one_shot_output, ensure_ascii=False, indent=2)}\n\n"
        f"CATALOG_CANDIDATES:\n{json.dumps(catalog_candidates, ensure_ascii=False, indent=2)}\n\n"
        f"CARD_JSON:\n{json.dumps(card, ensure_ascii=False, indent=2)}"
    )


def parse_json_array(text: str) -> list[object]:
    items, _parsed_items_key = parse_json_items_with_status(text)
    return items


def parse_json_items_with_status(text: str) -> tuple[list[object], bool]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        loaded = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", cleaned, re.DOTALL)
        if not match:
            return [], False
        try:
            loaded = json.loads(match.group(0))
        except json.JSONDecodeError:
            return [], False
    if isinstance(loaded, list):
        return loaded, False
    if isinstance(loaded, dict) and isinstance(loaded.get("items"), list):
        return loaded["items"], True
    return [], False


def normalize_listing_items(items: list[object]) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    allowed_types = {"game", "console", "controller", "accessory", "subscription", "digital_account", "service", "unknown"}
    allowed_conditions = {"new", "excellent", "good", "used", "broken", "unknown"}
    allowed_price_source_types = {
        "listing_price_single_item",
        "description_item_price",
        "description_total_lot_price",
        "description_each_price",
        "seller_says_price_varies",
        "unknown",
    }
    allowed_price_scopes = {"per_item", "total_lot", "each_item_same_price", "bundle_total", "unknown"}
    allowed_price_confidences = {"high", "medium", "low", "none"}
    allowed_lot_cost_sources = {
        "listing_lot_total",
        "description_total_lot_price",
        "pickup_discount_total",
        "explicit_discount_total",
        "unknown",
    }
    allowed_price_visibility = {
        "explicit_item_price",
        "listing_single_item_price",
        "total_lot_price",
        "price_in_photo",
        "price_in_private_messages",
        "price_varies",
        "no_price_visible",
        "unclear",
    }
    allowed_extraction_status = {
        "complete",
        "partial",
        "unclear",
        "photo_only",
        "blocked_by_missing_text",
        "blocked_by_junk_listing",
    }
    for item in items:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            continue
        name = re.sub(r"\s+", " ", str(item.get("name") or "")).strip()
        if not name:
            continue
        item_type = str(item.get("item_type") or "unknown").strip()
        condition = str(item.get("condition") or "unknown").strip()
        quantity = item.get("quantity", 1)
        try:
            quantity_int = max(1, int(quantity))
        except (TypeError, ValueError):
            quantity_int = 1
        price = item.get("price_rub")
        try:
            price_int = int(price) if price is not None and str(price).strip() else None
        except (TypeError, ValueError):
            price_int = None
        price_source_type = str(item.get("price_source_type") or "unknown").strip()
        price_scope = str(item.get("price_scope") or "unknown").strip()
        price_confidence = str(item.get("price_confidence") or "none").strip()
        lot_total_price = item.get("lot_total_price_rub")
        lot_unit_price = item.get("lot_unit_buy_price_rub")
        try:
            lot_total_price_int = int(lot_total_price) if lot_total_price is not None and str(lot_total_price).strip() else None
        except (TypeError, ValueError):
            lot_total_price_int = None
        try:
            lot_unit_price_int = int(lot_unit_price) if lot_unit_price is not None and str(lot_unit_price).strip() else None
        except (TypeError, ValueError):
            lot_unit_price_int = None
        lot_unit_confidence = str(item.get("lot_unit_buy_price_confidence") or "none").strip()
        lot_unit_source = str(item.get("lot_unit_buy_price_source_type") or "unknown").strip()
        normalized.append(
            {
                "name": name[:160],
                "canonical_name": re.sub(r"\s+", " ", str(item.get("canonical_name") or "")).strip()[:160] or None,
                "catalog_item_id": str(item.get("catalog_item_id") or "").strip()[:120] or None,
                "catalog_match_confidence": normalize_catalog_match_confidence(item.get("catalog_match_confidence")),
                "item_type": item_type if item_type in allowed_types else "unknown",
                "platform": str(item.get("platform") or "").strip()[:40] or None,
                "quantity": quantity_int,
                "condition": condition if condition in allowed_conditions else "unknown",
                "is_physical": item.get("is_physical") if isinstance(item.get("is_physical"), bool) else None,
                "price_rub": price_int,
                "price_source_type": price_source_type if price_source_type in allowed_price_source_types else "unknown",
                "price_scope": price_scope if price_scope in allowed_price_scopes else "unknown",
                "price_confidence": price_confidence if price_confidence in allowed_price_confidences else "none",
                "use_for_market_pricing": bool(item.get("use_for_market_pricing")),
                "lot_total_price_rub": lot_total_price_int,
                "lot_unit_buy_price_rub": lot_unit_price_int,
                "lot_unit_buy_price_confidence": lot_unit_confidence if lot_unit_confidence in allowed_price_confidences else "none",
                "lot_unit_buy_price_source_type": lot_unit_source if lot_unit_source in allowed_lot_cost_sources else "unknown",
                "use_for_lot_cost_estimate": bool(item.get("use_for_lot_cost_estimate")),
                "price_source_text": re.sub(r"\s+", " ", str(item.get("price_source_text") or "")).strip()[:240],
                "price_explanation": re.sub(r"\s+", " ", str(item.get("price_explanation") or "")).strip()[:320],
                "price_visibility": (
                    str(item.get("price_visibility") or "unclear").strip()
                    if str(item.get("price_visibility") or "unclear").strip() in allowed_price_visibility
                    else "unclear"
                ),
                "extraction_status": (
                    str(item.get("extraction_status") or "complete").strip()
                    if str(item.get("extraction_status") or "complete").strip() in allowed_extraction_status
                    else "unclear"
                ),
                "missing_data_reason": re.sub(r"\s+", " ", str(item.get("missing_data_reason") or "")).strip()[:320],
                "buyer_thoughts": re.sub(r"\s+", " ", str(item.get("buyer_thoughts") or "")).strip()[:320],
                "notes": re.sub(r"\s+", " ", str(item.get("notes") or "")).strip()[:240],
            }
        )
    return normalized[:MAX_NORMALIZED_ITEMS]


def extract_price_contexts(parts: list[object], *, limit: int = 30) -> list[str]:
    text = "\n".join(str(part or "") for part in parts)
    contexts: list[str] = []
    seen: set[str] = set()
    for raw_line in re.split(r"[\n\r;]+", text):
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        has_price = re.search(r"(?:\b\d{2,6}\s*(?:р|руб|₽|k|к)\b|\b\d+[,.]\d+\s*(?:т|тыс))", line, re.IGNORECASE)
        has_price_word = re.search(
            r"(?:цена|стоимость|за\s+все|за\s+всё|за\s+штуку|за\s+диск|кажд|все\s+вместе|комплект)",
            line,
            re.IGNORECASE,
        )
        if not (has_price or has_price_word):
            continue
        clipped = line[:260]
        key = clipped.lower()
        if key in seen:
            continue
        seen.add(key)
        contexts.append(clipped)
        if len(contexts) >= limit:
            break
    return contexts


def normalize_catalog_match_confidence(value: object) -> str:
    confidence = str(value or "none").strip().lower()
    if confidence in {"high", "medium", "low"}:
        return confidence
    return "none"


def fallback_listing_items_from_record(record: NewListingRecord) -> list[dict[str, object]]:
    title = re.sub(r"\s+", " ", str(getattr(record, "title", None) or "")).strip()
    if not title:
        return []
    lower = title.lower().replace("ё", "е")
    platform = infer_platform_from_text(title)
    item_type = "unknown"
    if any(token in lower for token in ("ps4", "ps5", "ps3", "playstation", "пристав", "консол")):
        item_type = "console" if any(token in lower for token in ("пристав", "консол", "playstation 4", "playstation 5", "ps4 slim", "ps4 pro", "ps5")) else "game"
    if any(token in lower for token in ("диск", "игра", "games", "game")):
        item_type = "game"
    if any(token in lower for token in ("игры", "диски")) and not any(token in lower for token in ("пристав", "консол")):
        title = f"Unknown {platform or ''} games".strip()
        item_type = "game"
    return [
        {
            "name": title[:160],
            "canonical_name": None,
            "catalog_item_id": None,
            "catalog_match_confidence": "none",
            "item_type": item_type,
            "platform": platform,
            "quantity": 1,
            "condition": "unknown",
            "is_physical": None,
            "price_rub": getattr(record, "price", None),
            "notes": "fallback_from_title",
        }
    ]


def complete_listing_items_from_source(
    record: NewListingRecord,
    items: list[dict[str, object]],
) -> list[dict[str, object]]:
    source_text = source_text_for_items(record)
    items = expand_little_nightmares_bundle(source_text, items)
    items = add_bonus_card_if_needed(source_text, items)
    items = adjust_quantity_from_simple_count(source_text, items)
    for item in items:
        if item.get("notes") == "price_line_chunk":
            continue
        apply_catalog_match_to_item(item)
    return items


def source_text_for_items(record: NewListingRecord) -> str:
    return " ".join(
        str(part or "")
        for part in [
            getattr(record, "title", None),
            getattr(record, "description", None),
            " ".join(getattr(record, "raw_card_texts", []) or []),
            " ".join(getattr(record, "raw_detail_texts", []) or []),
        ]
    )


def infer_platform_from_text(text: str) -> str | None:
    lower = text.lower()
    if "ps5" in lower or "playstation 5" in lower:
        return "PS5"
    if "ps4" in lower or "playstation 4" in lower:
        return "PS4"
    if "ps3" in lower or "playstation 3" in lower:
        return "PS3"
    if "ps2" in lower or "playstation 2" in lower:
        return "PS2"
    if "psp" in lower:
        return "PSP"
    return None


def important_title_tokens(value: object) -> set[str]:
    text = str(value or "").lower().replace("ё", "е")
    tokens: set[str] = set()
    for match in re.finditer(r"(?<![a-zа-я0-9])(fc|fifa|f1|ufc|nhl|nba|wrc)\s*([0-9]{1,2})(?![a-zа-я0-9])", text):
        tokens.add(f"{match.group(1)}{match.group(2)}")
    for match in re.finditer(r"(?<![a-zа-я0-9])([0-9]{4})(?![a-zа-я0-9])", text):
        year = int(match.group(1))
        if 1990 <= year <= 2035:
            tokens.add(str(year))
    for match in re.finditer(r"(?<![a-zа-я0-9])([2-9]|1[0-9])(?![a-zа-я0-9])", text):
        tokens.add(match.group(1))
    return tokens


def catalog_match_looks_wrong(item_name: object, candidate_name: object) -> bool:
    item_text = str(item_name or "").lower().replace("ё", "е")
    candidate_text = str(candidate_name or "").lower().replace("ё", "е")
    if not item_text or not candidate_text:
        return False
    if "gta" in item_text and "trilogy" in item_text and "mafia" in candidate_text:
        return True
    item_tokens = important_title_tokens(item_text)
    candidate_tokens = important_title_tokens(candidate_text)
    if item_tokens and candidate_tokens and item_tokens.isdisjoint(candidate_tokens):
        return True
    if item_tokens and not candidate_tokens:
        return True
    sport_prefixes = ("fc", "fifa", "f1", "ufc", "nhl", "nba", "wrc")
    for prefix in sport_prefixes:
        item_numbers = set(re.findall(rf"(?<![a-z0-9]){prefix}\s*([0-9]{{1,2}})(?![a-z0-9])", item_text))
        candidate_numbers = set(re.findall(rf"(?<![a-z0-9]){prefix}\s*([0-9]{{1,2}})(?![a-z0-9])", candidate_text))
        if item_numbers and candidate_numbers and item_numbers.isdisjoint(candidate_numbers):
            return True
    return False


def clear_catalog_match(item: dict[str, object]) -> None:
    item["catalog_item_id"] = None
    item["canonical_name"] = None
    item["catalog_match_confidence"] = "none"


def apply_catalog_match_to_item(item: dict[str, object]) -> None:
    name = str(item.get("name") or "")
    if not name:
        return
    item_type = str(item.get("item_type") or "")
    if item_type == "unknown":
        clear_catalog_match(item)
        return
    candidates = catalog_candidates_for_text(name, limit=8)
    if not candidates:
        return
    typed_candidates = [
        candidate
        for candidate in candidates
        if str(candidate.get("item_type") or "") == item_type and int(candidate.get("score") or 0) >= 10000
    ]
    top = typed_candidates[0] if typed_candidates else candidates[0]
    if int(top.get("score") or 0) < 10000:
        return
    current_id = str(item.get("catalog_item_id") or "")
    if current_id in GENERIC_CATALOG_ITEM_IDS and item_type != "console":
        clear_catalog_match(item)
        current_id = ""
    current_name = catalog_items_by_id().get(current_id, {}).get("canonical_name") if current_id else None
    if current_id and catalog_match_looks_wrong(name, current_name):
        clear_catalog_match(item)
        current_id = ""
    if current_id and current_id == str(top.get("catalog_item_id")):
        item["canonical_name"] = top.get("canonical_name")
        item["catalog_match_confidence"] = "high"
        return
    if catalog_match_looks_wrong(name, top.get("canonical_name")):
        if not current_id:
            return
        clear_catalog_match(item)
        return
    top_score = int(top.get("score") or 0)
    current_candidate = next(
        (candidate for candidate in candidates if str(candidate.get("catalog_item_id") or "") == current_id),
        None,
    )
    current_score = int(current_candidate.get("score") or 0) if current_candidate else 0
    current_catalog_item = catalog_items_by_id().get(current_id) if current_id else None
    current_item_type = str(current_catalog_item.get("item_type") or "") if current_catalog_item else ""
    top_item_type = str(top.get("item_type") or "")
    if current_item_type and item_type and current_item_type != item_type and top_item_type == item_type:
        current_id = ""
    if (
        current_id
        and current_id in catalog_items_by_id()
        and current_id not in BAD_CATALOG_ITEM_IDS
        and current_id not in GENERIC_CATALOG_ITEM_IDS
        and str(item.get("catalog_match_confidence")) == "high"
        and top_score < current_score + 250
    ):
        return
    item["catalog_item_id"] = top.get("catalog_item_id")
    item["canonical_name"] = top.get("canonical_name")
    item["catalog_match_confidence"] = "high"


def expand_little_nightmares_bundle(source_text: str, items: list[dict[str, object]]) -> list[dict[str, object]]:
    lower = source_text.lower()
    if not re.search(r"little\s+nightmares\s*(?:1\s*(?:&|and|и)\s*2|1/2)", lower):
        return items
    existing_ids = {str(item.get("catalog_item_id") or "") for item in items}
    if {"little_nightmares", "little_nightmares_2"} <= existing_ids:
        return items
    result = [
        item
        for item in items
        if "little nightmares" not in str(item.get("name") or "").lower()
        or str(item.get("catalog_item_id") or "") not in {"little_nightmares_complete_edition", ""}
    ]
    for item_id, name in (("little_nightmares", "Little Nightmares"), ("little_nightmares_2", "Little Nightmares 2")):
        if item_id in {str(item.get("catalog_item_id") or "") for item in result}:
            continue
        result.append(
            {
                "name": name,
                "canonical_name": catalog_items_by_id().get(item_id, {}).get("canonical_name") or name,
                "catalog_item_id": item_id,
                "catalog_match_confidence": "high",
                "item_type": "game",
                "platform": infer_platform_from_text(source_text) or "PS4",
                "quantity": 1,
                "condition": condition_from_source(source_text),
                "is_physical": True,
                "price_rub": None,
                "notes": "split_from_1_and_2",
            }
        )
    return result


def add_bonus_card_if_needed(source_text: str, items: list[dict[str, object]]) -> list[dict[str, object]]:
    lower = source_text.lower().replace("ё", "е")
    has_card_marker = "+карта" in lower or "с картой" in lower or "includes card" in lower
    if not has_card_marker or "карта памяти" in lower:
        return items
    if any("карт" in str(item.get("name") or "").lower() or "card" in str(item.get("name") or "").lower() for item in items):
        return items
    return [
        *items,
        {
            "name": "Bonus card",
            "canonical_name": None,
            "catalog_item_id": None,
            "catalog_match_confidence": "none",
            "item_type": "accessory",
            "platform": infer_platform_from_text(source_text),
            "quantity": 1,
            "condition": "unknown",
            "is_physical": True,
            "price_rub": None,
            "notes": "bonus card mentioned in listing",
        },
    ]


def adjust_quantity_from_simple_count(source_text: str, items: list[dict[str, object]]) -> list[dict[str, object]]:
    if len(items) != 1:
        return items
    lower = source_text.lower().replace("ё", "е")
    match = re.search(r"\b(?:два|2)\s+диск", lower)
    if not match:
        return items
    item = dict(items[0])
    if item.get("item_type") == "game":
        item["quantity"] = max(int(item.get("quantity") or 1), 2)
        item["notes"] = re.sub(r"\s+", " ", f"{item.get('notes') or ''} quantity inferred from text").strip()
        return [item]
    return items


def condition_from_source(source_text: str) -> str:
    lower = source_text.lower().replace("ё", "е")
    if "нов" in lower or "запечат" in lower:
        return "new"
    if "б/у" in lower or "бу" in lower:
        return "used"
    return "unknown"


MARKET_PRICE_ITEM_TYPES = {"game", "console", "controller", "accessory"}
MARKET_PRICE_SOURCE_TYPES = {"listing_price_single_item", "description_item_price", "description_each_price"}
MARKET_PRICE_SCOPES = {"per_item", "each_item_same_price"}
MARKET_PRICE_CONFIDENCES = {"high", "medium"}
NON_MARKET_ITEM_TYPES = {"digital_account", "subscription", "service", "unknown"}


def product_name_is_concrete(name: object) -> bool:
    normalized = re.sub(r"\s+", " ", str(name or "").lower().replace("ё", "е")).strip()
    if not normalized:
        return False
    unknown_markers = (
        "unknown",
        "unnamed",
        "not listed",
        "photo",
        "photos",
        "\u043d\u0435\u0438\u0437\u0432\u0435\u0441\u0442",
        "\u043d\u0435 \u0443\u043a\u0430\u0437",
        "\u043d\u0430 \u0444\u043e\u0442",
        "\u0441 \u0444\u043e\u0442",
        "\u043d\u0435 \u043f\u0435\u0440\u0435\u0447\u0438\u0441\u043b",
    )
    if any(marker in normalized for marker in unknown_markers):
        return False
    words = set(re.findall(r"[0-9a-zа-я]+", normalized))
    generic_words = {
        "ps4",
        "ps5",
        "playstation",
        "4",
        "5",
        "\u0438\u0433\u0440\u0430",
        "\u0438\u0433\u0440\u044b",
        "\u0434\u0438\u0441\u043a",
        "\u0434\u0438\u0441\u043a\u0438",
        "\u043b\u043e\u0442",
        "\u043f\u0430\u0447\u043a\u0430",
        "\u043a\u043e\u043c\u043f\u043b\u0435\u043a\u0442",
    }
    return bool(words - generic_words)


def item_market_price_rejection_reason(item: dict[str, object], *, item_count: int) -> str | None:
    item_type = str(item.get("item_type") or "unknown")
    if item_type in NON_MARKET_ITEM_TYPES:
        return "not a physical concrete market-priced item"
    if item_type not in MARKET_PRICE_ITEM_TYPES:
        return "unsupported item type for market pricing"
    if item.get("is_physical") is False:
        return "not physical"
    if not product_name_is_concrete(item.get("name")):
        return "item name is unknown or too generic"
    price = item.get("price_rub")
    try:
        price_int = int(price) if price is not None else None
    except (TypeError, ValueError):
        price_int = None
    if price_int is None or price_int <= 0:
        return "no reliable item price"
    source_type = str(item.get("price_source_type") or "unknown")
    if source_type not in MARKET_PRICE_SOURCE_TYPES:
        return "price belongs to lot/bundle or is unclear"
    scope = str(item.get("price_scope") or "unknown")
    if scope not in MARKET_PRICE_SCOPES:
        return "price is not per-item"
    confidence = str(item.get("price_confidence") or "none")
    if confidence not in MARKET_PRICE_CONFIDENCES:
        return "price confidence is too low"
    if item_count > 1 and source_type == "listing_price_single_item":
        return "listing price cannot price multiple extracted items"
    return None


def enforce_market_price_flags(items: list[dict[str, object]]) -> None:
    item_count = len(items)
    for item in items:
        reason = item_market_price_rejection_reason(item, item_count=item_count)
        if reason:
            item["use_for_market_pricing"] = False
            existing = str(item.get("price_explanation") or "").strip()
            if reason not in existing:
                item["price_explanation"] = re.sub(
                    r"\s+",
                    " ",
                    f"{existing} Not usable for CRM market pricing: {reason}.".strip(),
                )[:320]
            continue
        item["use_for_market_pricing"] = True


def apply_listing_price_to_single_concrete_item(
    items: list[dict[str, object]],
    *,
    listing_price: int | None,
) -> None:
    if listing_price is None or listing_price <= 0:
        return
    concrete_items = [
        item
        for item in items
        if isinstance(item, dict)
        and str(item.get("item_type") or "unknown") in MARKET_PRICE_ITEM_TYPES
        and item.get("is_physical") is not False
        and product_name_is_concrete(item.get("name"))
    ]
    if len(concrete_items) != 1:
        return
    item = concrete_items[0]
    if item.get("price_rub") is not None:
        return
    item["price_rub"] = listing_price
    item["price_source_type"] = "listing_price_single_item"
    item["price_scope"] = "per_item"
    item["price_confidence"] = "high"
    item["price_source_text"] = item.get("price_source_text") or f"listing price {listing_price}"
    item["price_explanation"] = "Single concrete product in listing; listing price belongs to this item."


def round_rub(value: float) -> int:
    return int(round(value))


def text_mentions_heavy_rain_beyond_collection(text: str) -> bool:
    normalized = normalize_catalog_text(text)
    if "heavy rain" not in normalized:
        return False
    if "beyond two souls" in normalized:
        return True
    return "\u0437\u0430 \u0433\u0440\u0430\u043d\u044c\u044e \u0434\u0432\u0435 \u0434\u0443\u0448\u0438" in normalized


def item_mentions_heavy_rain_beyond_part(item: dict[str, object]) -> bool:
    text = normalize_catalog_text(
        " ".join(str(part or "") for part in [item.get("name"), item.get("canonical_name"), item.get("catalog_item_id")])
    )
    if text_mentions_heavy_rain_beyond_collection(text):
        return True
    has_heavy = "heavy rain" in text or "heavy_rain" in text
    has_beyond = "beyond two souls" in text or "beyond_two_souls" in text
    return has_heavy or has_beyond


def merge_heavy_rain_beyond_collection(
    record: NewListingRecord,
    items: list[dict[str, object]],
    *,
    listing_price: int | None,
) -> list[dict[str, object]]:
    source_text = " ".join(
        str(part or "")
        for part in [
            getattr(record, "title", None),
            getattr(record, "description", None),
            " ".join(getattr(record, "raw_card_texts", []) or []),
            " ".join(getattr(record, "raw_detail_texts", []) or []),
        ]
    )
    if not text_mentions_heavy_rain_beyond_collection(source_text):
        return items

    matched = [item for item in items if isinstance(item, dict) and item_mentions_heavy_rain_beyond_part(item)]
    if not matched:
        return items

    first = dict(matched[0])
    remaining = [item for item in items if not (isinstance(item, dict) and item_mentions_heavy_rain_beyond_part(item))]
    first.update(
        {
            "name": HEAVY_RAIN_BEYOND_NAME,
            "canonical_name": HEAVY_RAIN_BEYOND_NAME,
            "catalog_item_id": HEAVY_RAIN_BEYOND_ID,
            "catalog_match_confidence": "high",
            "item_type": "game",
            "platform": "PS4",
            "quantity": 1,
            "is_physical": True,
            "lot_total_price_rub": None,
            "lot_unit_buy_price_rub": None,
            "lot_unit_buy_price_confidence": "none",
            "lot_unit_buy_price_source_type": "unknown",
            "use_for_lot_cost_estimate": False,
        }
    )
    if listing_price is not None and listing_price > 0 and not remaining:
        first.update(
            {
                "price_rub": listing_price,
                "price_source_type": "listing_price_single_item",
                "price_scope": "per_item",
                "price_confidence": "high",
                "use_for_market_pricing": True,
                "price_source_text": f"listing price {listing_price}",
                "price_explanation": (
                    "Heavy Rain And Beyond Two Souls is one physical collection/SKU; "
                    "listing price belongs to this exact product."
                ),
            }
        )
    else:
        first.update(
            {
                "price_rub": None,
                "price_source_type": "unknown",
                "price_scope": "unknown",
                "price_confidence": "none",
                "use_for_market_pricing": False,
                "price_explanation": (
                    "Heavy Rain And Beyond Two Souls is one physical collection/SKU; "
                    "price is not safely assignable because other products are present."
                ),
            }
        )
    return [*remaining, first]


def prices_near_lot_markers(source_text: str) -> list[tuple[int, str, str]]:
    text = source_text.lower().replace("ё", "е").replace("С‘", "Рµ")
    marker_pattern = (
        r"(?:"
        r"за\s+все|за\s+всё|все\s+вместе|всё\s+вместе|цена\s+за\s+все|цена\s+за\s+всё|"
        r"забер[её]те\s+все|забер[её]те\s+всё|оптом|комплектом"
        r")"
    )
    price_pattern = r"(?<!\d)(\d{2,6})(?!\d)\s*(?:р|руб|₽)?"
    results: list[tuple[int, str, str]] = []
    fragments = [line.strip() for line in re.split(r"[\n\r;]+", text) if line.strip()]
    if not fragments:
        fragments = [re.sub(r"\s+", " ", text).strip()]
    for raw_fragment in fragments:
        fragment = re.sub(r"\s+", " ", raw_fragment).strip()
        if not fragment:
            continue
        lower_fragment = fragment.lower()
        if any(marker in lower_fragment for marker in ("доставка", "пункт самовывоза", "авито доставка")):
            continue
        has_total_marker = re.search(marker_pattern, fragment, flags=re.IGNORECASE)
        has_pickup_discount = (
            ("самовывоз" in lower_fragment or "самовынос" in lower_fragment)
            and ("скид" in lower_fragment or "дешев" in lower_fragment)
        )
        if not has_total_marker and not has_pickup_discount:
            continue
        for price_match in re.finditer(price_pattern, fragment, flags=re.IGNORECASE):
            try:
                price = int(price_match.group(1))
            except (TypeError, ValueError):
                continue
            if price < 100:
                continue
            source_type = "pickup_discount_total" if has_pickup_discount else "explicit_discount_total"
            if "за все" in fragment or "за всё" in fragment or "вместе" in fragment or "оптом" in fragment:
                source_type = "description_total_lot_price"
            results.append((price, source_type, fragment[:220]))
    return results


def game_lot_quantity(items: list[dict[str, object]]) -> int:
    total = 0
    for item in items:
        if str(item.get("item_type") or "") != "game":
            continue
        if item.get("is_physical") is False:
            continue
        total += safe_int(item.get("quantity"), 1)
    return total


def choose_lot_total_price(
    *,
    record: NewListingRecord,
    items: list[dict[str, object]],
    listing_price: int | None,
) -> tuple[int | None, str, str]:
    source_text = "\n".join(
        str(part or "")
        for part in (getattr(record, "title", None), getattr(record, "description", None))
    )
    candidates = prices_near_lot_markers(source_text)
    if candidates:
        price, source_type, fragment = min(candidates, key=lambda candidate: candidate[0])
        return price, source_type, fragment
    explicit_total_items = [
        item
        for item in items
        if item.get("price_rub") is not None
        and str(item.get("price_scope") or "") in {"total_lot", "bundle_total"}
    ]
    if explicit_total_items:
        item = explicit_total_items[0]
        try:
            return int(item.get("price_rub")), "description_total_lot_price", str(item.get("price_source_text") or "")[:220]
        except (TypeError, ValueError):
            pass
    has_item_specific_prices = any(
        item.get("price_rub") is not None
        and str(item.get("price_scope") or "") in MARKET_PRICE_SCOPES
        and str(item.get("price_source_type") or "") in MARKET_PRICE_SOURCE_TYPES
        for item in items
    )
    if has_item_specific_prices:
        return None, "unknown", ""
    if listing_price is not None and game_lot_quantity(items) >= 2:
        return listing_price, "listing_lot_total", f"listing price {listing_price}"
    return None, "unknown", ""


def apply_lot_cost_estimate_to_games(
    record: NewListingRecord,
    items: list[dict[str, object]],
    *,
    listing_price: int | None,
) -> None:
    if is_console_bundle(record, items):
        return
    quantity = game_lot_quantity(items)
    if quantity < 2:
        return
    lot_total, source_type, source_text = choose_lot_total_price(record=record, items=items, listing_price=listing_price)
    if lot_total is None or lot_total <= 0:
        return
    unit_price = round_rub(lot_total / quantity)
    confidence = "medium" if source_type == "listing_lot_total" else "high"
    for item in items:
        if str(item.get("item_type") or "") != "game" or item.get("is_physical") is False:
            continue
        item["lot_total_price_rub"] = lot_total
        item["lot_unit_buy_price_rub"] = unit_price
        item["lot_unit_buy_price_confidence"] = confidence
        item["lot_unit_buy_price_source_type"] = source_type
        item["use_for_lot_cost_estimate"] = product_name_is_concrete(item.get("name"))
        existing = str(item.get("price_explanation") or "").strip()
        lot_explanation = (
            f"Approximate lot buy cost: {lot_total} / {quantity} games = {unit_price} per game; "
            f"source={source_type}."
        )
        if lot_explanation not in existing:
            item["price_explanation"] = re.sub(r"\s+", " ", f"{existing} {lot_explanation}".strip())[:320]
        if source_text and not item.get("price_source_text"):
            item["price_source_text"] = source_text[:240]


def is_console_bundle(record: NewListingRecord, items: list[dict[str, object]]) -> bool:
    if str(getattr(record, "item_kind", "") or "") == "console":
        return True
    return any(str(item.get("item_type") or "") == "console" for item in items if isinstance(item, dict))


def clear_console_bundle_game_lot_costs(
    record: NewListingRecord,
    items: list[dict[str, object]],
) -> None:
    if not is_console_bundle(record, items):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("item_type") or "") != "game":
            continue
        source_type = str(item.get("lot_unit_buy_price_source_type") or "")
        if source_type in {"listing_lot_total", "unknown"} or item.get("price_scope") == "bundle_total":
            item["lot_total_price_rub"] = None
            item["lot_unit_buy_price_rub"] = None
            item["lot_unit_buy_price_confidence"] = "none"
            item["lot_unit_buy_price_source_type"] = "unknown"
            item["use_for_lot_cost_estimate"] = False


def interpret_listing_price_for_record(record: NewListingRecord) -> dict[str, object]:
    items = getattr(record, "llm_extracted_items", None)
    if not isinstance(items, list):
        items = []
    listing_price = getattr(record, "price", None)
    try:
        listing_price_int = int(listing_price) if listing_price is not None else None
    except (TypeError, ValueError):
        listing_price_int = None
    concrete_items = [item for item in items if isinstance(item, dict) and product_name_is_concrete(item.get("name"))]
    item_specific_prices = [
        item
        for item in concrete_items
        if item.get("price_rub") is not None
        and str(item.get("price_scope") or "") in MARKET_PRICE_SCOPES
        and str(item.get("price_source_type") or "") in MARKET_PRICE_SOURCE_TYPES
    ]
    market_observations = [item for item in concrete_items if item.get("use_for_market_pricing") is True]
    lot_cost_observations = [item for item in concrete_items if item.get("use_for_lot_cost_estimate") is True]
    item_types = {str(item.get("item_type") or "unknown") for item in concrete_items}
    source_text = source_text_for_items(record).lower().replace("ё", "е").replace("С‘", "Рµ")
    total_markers = (
        "for all",
        "all together",
        "\u0437\u0430 \u0432\u0441\u0435",
        "\u0437\u0430 \u0432\u0441\u0451",
        "\u0432\u0441\u0435 \u0432\u043c\u0435\u0441\u0442\u0435",
        "\u0446\u0435\u043d\u0430 \u0437\u0430 \u0432\u0441\u0435",
        "\u0446\u0435\u043d\u0430 \u0437\u0430 \u0432\u0441\u0451",
        "\u043a\u043e\u043c\u043f\u043b\u0435\u043a\u0442\u043e\u043c",
    )
    each_markers = (
        "each",
        "\u043a\u0430\u0436\u0434",
        "\u0437\u0430 \u0434\u0438\u0441\u043a",
        "\u0437\u0430 \u0448\u0442",
        "\u043f\u043e \u0446\u0435\u043d\u0435",
    )
    attention_markers = (
        "\u0446\u0435\u043d\u044b \u0440\u0430\u0437\u043d\u044b\u0435",
        "\u0446\u0435\u043d\u0430 \u0443\u0441\u043b\u043e\u0432\u043d",
        "\u0446\u0435\u043d\u0430 \u0434\u043b\u044f \u043f\u0440\u0438\u0432\u043b\u0435\u0447",
        "\u0443\u0442\u043e\u0447\u043d",
    )

    if listing_price_int is None:
        role = "missing"
        can_use_listing_price = False
        explanation = "Listing price was not extracted."
    elif len(concrete_items) == 1 and not (item_types & {"digital_account", "subscription", "service"}):
        role = "single_item_price"
        can_use_listing_price = True
        explanation = "Listing price most likely belongs to the only concrete product."
    elif any(marker in source_text for marker in total_markers) or "console" in item_types:
        role = "lot_or_bundle_total_price"
        can_use_listing_price = False
        explanation = "Listing price describes the whole lot/bundle, not each product."
    elif any(marker in source_text for marker in attention_markers) or (listing_price_int <= 100 and len(concrete_items) > 1):
        role = "attention_or_placeholder_price"
        can_use_listing_price = False
        explanation = "Listing price looks like a placeholder/attention price."
    elif item_specific_prices:
        role = "listing_price_unused_item_prices_in_description"
        can_use_listing_price = False
        explanation = "Description has item-specific prices; listing price is not needed for item market observations."
    elif len(concrete_items) > 1 and any(marker in source_text for marker in each_markers):
        role = "same_price_for_each_item_possible"
        can_use_listing_price = False
        explanation = "Text hints at same per-item price, but CRM should trust extracted item prices only if they are explicit."
    elif len(concrete_items) > 1:
        role = "multi_item_price_unclear"
        can_use_listing_price = False
        explanation = "Multiple products found and listing price cannot be safely assigned to one product."
    else:
        role = "unknown"
        can_use_listing_price = False
        explanation = "Not enough structure to decide what the listing price means."

    return {
        "listing_price_rub": listing_price_int,
        "role": role,
        "can_use_listing_price_for_market_pricing": can_use_listing_price,
        "concrete_items_count": len(concrete_items),
        "item_specific_prices_count": len(item_specific_prices),
        "market_price_observations_count": len(market_observations),
        "lot_cost_observations_count": len(lot_cost_observations),
        "explanation": explanation,
    }


def sanitize_listing_items_for_record(
    record: NewListingRecord,
    items: list[dict[str, object]],
) -> list[dict[str, object]]:
    if not items:
        return items
    listing_price = getattr(record, "price", None)
    try:
        listing_price_int = int(listing_price) if listing_price is not None else None
    except (TypeError, ValueError):
        listing_price_int = None
    if len(items) > 1 and listing_price_int is not None:
        for item in items:
            if item.get("price_rub") == listing_price_int:
                item["price_rub"] = None
                item["price_source_type"] = "description_total_lot_price"
                item["price_scope"] = "total_lot"
                item["price_confidence"] = "none"
                item["use_for_market_pricing"] = False
                item["price_explanation"] = "Listing price belongs to multiple products, not this exact item."

    source_text = " ".join(
        str(part or "")
        for part in [
            getattr(record, "title", None),
            getattr(record, "description", None),
            " ".join(getattr(record, "raw_card_texts", []) or []),
            " ".join(getattr(record, "raw_detail_texts", []) or []),
        ]
    ).lower()
    condition_markers = {
        "new": ("нов", "запечат", "пломб"),
        "excellent": ("отлич", "идеал"),
        "good": ("хорош", "без дефект", "без царап", "без проблем"),
        "used": ("б/у", "бу", "пользов", "следы", "потерт", "потёрт"),
        "broken": ("не рабоч", "нерабоч", "сломан", "ремонт", "дефект"),
    }
    for item in items:
        item_type = str(item.get("item_type") or "")
        if item_type in {"digital_account", "subscription", "service"}:
            item["is_physical"] = False
            item["use_for_market_pricing"] = False
        catalog_item_id = item.get("catalog_item_id")
        if str(catalog_item_id or "") in GENERIC_CATALOG_ITEM_IDS and item_type != "console":
            item["catalog_item_id"] = None
            item["canonical_name"] = None
            item["catalog_match_confidence"] = "none"
            continue
        if item_type == "unknown":
            item["catalog_item_id"] = None
            item["canonical_name"] = None
            item["catalog_match_confidence"] = "none"
            continue
        catalog_item = catalog_items_by_id().get(str(catalog_item_id)) if catalog_item_id else None
        if not catalog_item:
            clear_catalog_match(item)
        else:
            item["canonical_name"] = catalog_item.get("canonical_name") or item.get("canonical_name")
            if catalog_match_looks_wrong(item.get("name"), item.get("canonical_name")):
                clear_catalog_match(item)
                continue
        condition = str(item.get("condition") or "unknown")
        markers = condition_markers.get(condition)
        if markers and not any(marker in source_text for marker in markers):
            item["condition"] = "unknown"
    items = merge_heavy_rain_beyond_collection(record, items, listing_price=listing_price_int)
    apply_listing_price_to_single_concrete_item(items, listing_price=listing_price_int)
    apply_lot_cost_estimate_to_games(record, items, listing_price=listing_price_int)
    clear_console_bundle_game_lot_costs(record, items)
    enforce_market_price_flags(items)
    result = deduplicate_listing_items(items)
    result = merge_heavy_rain_beyond_collection(record, result, listing_price=listing_price_int)
    apply_listing_price_to_single_concrete_item(result, listing_price=listing_price_int)
    apply_lot_cost_estimate_to_games(record, result, listing_price=listing_price_int)
    clear_console_bundle_game_lot_costs(record, result)
    enforce_market_price_flags(result)
    return result


def deduplicate_listing_items(items: list[dict[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen: dict[tuple[str, str], int] = {}
    for item in items:
        key = listing_item_identity(item)
        existing_index = seen.get(key)
        if existing_index is None:
            seen[key] = len(result)
            result.append(item)
            continue
        existing = result[existing_index]
        existing["quantity"] = max(safe_int(existing.get("quantity"), 1), safe_int(item.get("quantity"), 1))
        if not existing.get("price_rub") and item.get("price_rub"):
            existing["price_rub"] = item.get("price_rub")
            for key in (
                "price_source_type",
                "price_scope",
                "price_confidence",
                "use_for_market_pricing",
                "price_source_text",
                "price_explanation",
            ):
                if item.get(key) is not None:
                    existing[key] = item.get(key)
        if not existing.get("lot_unit_buy_price_rub") and item.get("lot_unit_buy_price_rub"):
            for key in (
                "lot_total_price_rub",
                "lot_unit_buy_price_rub",
                "lot_unit_buy_price_confidence",
                "lot_unit_buy_price_source_type",
                "use_for_lot_cost_estimate",
                "price_source_text",
                "price_explanation",
            ):
                if item.get(key) is not None:
                    existing[key] = item.get(key)
        if not existing.get("notes") and item.get("notes"):
            existing["notes"] = item.get("notes")
    return result


def listing_item_identity(item: dict[str, object]) -> tuple[str, str]:
    item_type = str(item.get("item_type") or "unknown")
    catalog_item_id = str(item.get("catalog_item_id") or "").strip()
    if catalog_item_id:
        return item_type, "catalog:" + catalog_item_id
    name = re.sub(r"[^0-9a-zа-я]+", " ", str(item.get("name") or "").lower().replace("ё", "е")).strip()
    return item_type, "name:" + name


def safe_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def analyze_listing(
    record: NewListingRecord,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    api_url: str = OPENROUTER_URL,
) -> dict[str, object]:
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Ты анализируешь объявления Авито по играм и приставкам. "
                    "Отвечай только валидным JSON без markdown. "
                    "Не выдумывай данные, если в описании не хватает информации."
                ),
            },
            {"role": "user", "content": build_prompt(record)},
        ],
        "temperature": 0.1,
        "max_tokens": 700,
    }
    request = urllib.request.Request(
        api_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost/monitor",
            "X-Title": "Monitor",
        },
        method="POST",
    )
    response_result = openrouter_response_body(request, model)
    if isinstance(response_result, dict):
        return response_result
    body = response_result

    try:
        data = json.loads(body)
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, json.JSONDecodeError, TypeError) as error:
        result = llm_error(model, f"Bad LLM response: {error}", "llm_bad_response")
        result["raw"] = body[:1000]
        return result
    if not isinstance(content, str) or not content.strip():
        result = llm_error(model, "Bad LLM response: empty message content", "llm_empty_response")
        result["raw"] = body[:1000]
        return result

    parsed = parse_json_object(content)
    parsed["ok"] = True
    parsed["model"] = model
    if not parsed.get("verdict"):
        parsed["verdict"] = "unclear"
        parsed.setdefault("errors", ["llm_unstructured_output"])
        parsed.setdefault("notes", str(parsed.get("raw_result", ""))[:500])
    return parsed


def openrouter_response_body(request: urllib.request.Request, model: str) -> str | dict[str, object]:
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            retry_after = retry_after_seconds(error, detail)
            if error.code == 429 and attempt == 0 and retry_after is not None and retry_after <= 35:
                time.sleep(retry_after + 1)
                continue
            return llm_error(model, f"HTTP {error.code}: {detail[:500]}", "llm_http_error")
        except OSError as error:
            fallback = polza_dns_fallback_response(request, timeout=40)
            if fallback is not None:
                status, body = fallback
                if 200 <= status < 300:
                    return body
                return llm_error(model, f"HTTP {status}: {body[:500]}", "llm_http_error")
            return llm_error(model, str(error), "llm_network_error")
    return llm_error(model, "LLM request failed after retry", "llm_retry_failed")


def openrouter_completion_body(
    payload: dict[str, object],
    *,
    api_key: str,
    model: str,
    api_url: str = OPENROUTER_URL,
    request_timeout_seconds: int = 60,
) -> str | dict[str, object]:
    current_payload = dict(payload)
    retried_without_json_mode = False
    for attempt in range(2):
        request = urllib.request.Request(
            api_url,
            data=json.dumps(current_payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "http://localhost/monitor",
                "X-Title": "Monitor",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=request_timeout_seconds) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            if (
                not retried_without_json_mode
                and "response_format" in current_payload
                and error.code in {400, 404, 422}
                and "response_format" in detail.lower()
            ):
                current_payload.pop("response_format", None)
                retried_without_json_mode = True
                continue
            retry_after = retry_after_seconds(error, detail)
            if error.code == 429 and attempt == 0 and retry_after is not None and retry_after <= 35:
                time.sleep(retry_after + 1)
                continue
            return llm_error(model, f"HTTP {error.code}: {detail[:500]}", "llm_http_error")
        except OSError as error:
            fallback = polza_dns_fallback_response(request, timeout=request_timeout_seconds)
            if fallback is not None:
                status, body = fallback
                if 200 <= status < 300:
                    return body
                return llm_error(model, f"HTTP {status}: {body[:500]}", "llm_http_error")
            return llm_error(model, str(error), "llm_network_error")
    return llm_error(model, "LLM request failed after retry", "llm_retry_failed")


def polza_dns_fallback_response(request: urllib.request.Request, *, timeout: int) -> tuple[int, str] | None:
    parsed = urllib.parse.urlparse(request.full_url)
    if parsed.hostname != "polza.ai":
        return None
    try:
        socket.getaddrinfo("polza.ai", 443)
        return None
    except OSError:
        pass
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    headers = dict(request.header_items())
    headers["Host"] = "polza.ai"
    try:
        connection = FixedSNIHTTPSConnection("polza.ai", POLZA_FALLBACK_IP, timeout=timeout)
        connection.request(request.get_method(), path, body=request.data, headers=headers)
        response = connection.getresponse()
        body = response.read().decode("utf-8", errors="replace")
        connection.close()
        return response.status, body
    except OSError:
        return None


class FixedSNIHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, fixed_ip: str, **kwargs: object) -> None:
        super().__init__(host, **kwargs)
        self.fixed_ip = fixed_ip

    def connect(self) -> None:
        sock = socket.create_connection((self.fixed_ip, self.port), self.timeout, self.source_address)
        context = ssl.create_default_context()
        self.sock = context.wrap_socket(sock, server_hostname=self.host)


def retry_after_seconds(error: urllib.error.HTTPError, detail: str) -> int | None:
    header = error.headers.get("Retry-After")
    if header and header.isdigit():
        return int(header)
    try:
        payload = json.loads(detail)
        metadata = payload.get("error", {}).get("metadata", {})
        raw = metadata.get("retry_after_seconds")
        if isinstance(raw, (int, float)):
            return int(raw)
    except (json.JSONDecodeError, AttributeError):
        pass
    return None


def llm_error(model: str, message: str, code: str = "llm_error") -> dict[str, object]:
    return {
        "ok": False,
        "model": model,
        "verdict": "llm_error",
        "errors": [code],
        "notes": message[:500],
        "error": message,
    }


def build_prompt(record: NewListingRecord) -> str:
    data = {
        "title": record.title,
        "price_rub": record.price,
        "address": record.address,
        "seller_city": record.seller_city,
        "item_kind_hint": record.item_kind,
        "delivery_text_from_search_card": record.delivery_text,
        "description": record.description,
        "url": record.url,
    }
    schema = {
        "verdict": "worth_considering | trash | unclear",
        "interest_label": "интересно | не интересно | непонятно",
        "summary": "коротко что продают",
        "item_kind": "console | game | mixed | other | unclear",
        "seller_city": "город продавца из входных данных или null",
        "lot_type": "single_item | bundle | many_items | digital_account_or_key | unclear",
        "is_good_deal": "yes | no | unclear",
        "main_prices": ["извлечённые цены и за что они"],
        "effective_price_rub": "цена объявления или сумма цен игр из описания, если она явно считается",
        "price_basis": "listing_price | description_sum | description_prices | unknown",
        "delivery_available": "yes | no | unclear",
        "delivery_terms": "стоимость/срок/способы доставки из delivery_text или описания",
        "urgency": "yes | no | unclear",
        "errors": ["title_description_mismatch", "incomplete_description", "price_mismatch", "suspicious_digital", "other"],
        "notes": "1-3 предложения для человека",
    }
    return (
        "Проанализируй объявление. Нужно понять: халява или нет, один товар или список, "
        "какие цены указаны, есть ли срочность, доставка, ошибки/несостыковки.\n\n"
        "Правила интереса:\n"
        "- Если это приставка/консоль и seller_city = Москва, interest_label = интересно.\n"
        "- Если это приставка/консоль и seller_city не Москва, interest_label = не интересно.\n"
        "- Если это игра/диск/несколько игр, interest_label = интересно независимо от города.\n"
        "- Если тип товара непонятен, interest_label = непонятно.\n\n"
        "Правила цены и доставки для игр:\n"
        "- Для игр обязательно оцени доставку по delivery_text_from_search_card и описанию.\n"
        "- Если в описании есть цены отдельных игр, перечисли их в main_prices и по возможности посчитай сумму в effective_price_rub; price_basis = description_sum.\n"
        "- Если отдельных цен нет, используй price_rub как effective_price_rub; price_basis = listing_price.\n"
        "- В delivery_terms напиши цену, срок и способ доставки, если они явно есть.\n\n"
        f"Верни JSON строго по схеме:\n{json.dumps(schema, ensure_ascii=False)}\n\n"
        f"Объявление:\n{json.dumps(data, ensure_ascii=False, indent=2)}"
    )


def parse_json_object(text: str) -> dict[str, object]:
    try:
        loaded = json.loads(text)
        return loaded if isinstance(loaded, dict) else {"raw_result": loaded}
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                loaded = json.loads(match.group(0))
                return loaded if isinstance(loaded, dict) else {"raw_result": loaded}
            except json.JSONDecodeError:
                pass
    return {"raw_result": text}


def record_to_analysis_input(record: NewListingRecord) -> dict[str, Any]:
    return asdict(record)
