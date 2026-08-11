from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.request
import uuid
from typing import Any

from .env import get_env
from .listing_db import heuristic_condition_flags
from .monitor import NewListingRecord


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


VERDICT_LABELS = {
    "worth_considering": "стоит посмотреть",
    "trash": "мусор",
    "unclear": "непонятно",
    "llm_error": "ошибка LLM",
}

INTEREST_LABELS = {
    "yes": "да",
    "no": "нет",
    "unclear": "непонятно",
    "интересно": "интересно",
    "не интересно": "не интересно",
    "непонятно": "непонятно",
}

SCENARIO_LABELS = {
    "single_game": "один диск",
    "game_bundle": "пачка дисков",
    "unknown_discs": "диски без понятных названий",
    "console_only": "только приставка",
    "console_bundle": "приставка с комплектом",
    "catalog_or_price_list": "прайс-лист/магазин",
    "excluded_service_or_rental": "аренда/скупка/сервис",
    "game_lot_total_price": "лот игр, цена за всё",
    "game_lot_individual_prices": "лот игр, цены по играм",
    "game_lot_unknown_structure": "лот игр, структура непонятна",
    "digital_account": "цифровой аккаунт/аренда",
    "accessory": "аксессуар",
    "unknown": "непонятный сценарий",
}

PRICE_BASIS_LABELS = {
    "listing_price": "цена объявления",
    "description_sum": "сумма цен из описания",
    "description_prices": "цены из описания",
    "unknown": "непонятно",
}

DECISION_LABELS = {
    "GOOD": "хорошо",
    "CHECK": "проверь",
    "SKIP": "пропуск",
}


def telegram_enabled(settings: dict[str, object]) -> bool:
    integrations = settings.get("integrations", {})
    if not isinstance(integrations, dict):
        return False
    telegram = integrations.get("telegram", {})
    if not isinstance(telegram, dict):
        return False
    return bool(telegram.get("enabled", False))


def telegram_chat_ids() -> list[str]:
    values: list[str] = []
    single = get_env("AVITO_TG_CHAT_ID")
    if single:
        values.append(single)
    multiple = get_env("AVITO_TG_CHAT_IDS")
    if multiple:
        values.extend(part.strip() for part in str(multiple).split(",") if part.strip())
    artem = get_env("AVITO_TG_ARTEM_CHAT_ID") or get_env("ARTEM_TG_CHAT_ID")
    if artem:
        values.append(artem)

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        chat_id = str(value).strip()
        if chat_id and chat_id not in seen:
            seen.add(chat_id)
            result.append(chat_id)
    return result


def _telegram_credentials(settings: dict[str, object]) -> tuple[str | None, list[str], dict[str, object] | None]:
    if not telegram_enabled(settings):
        return None, [], None
    token = get_env("AVITO_TG_BOT_TOKEN")
    if not token:
        return None, [], {"ok": False, "error": "AVITO_TG_BOT_TOKEN is not set"}
    chat_ids = telegram_chat_ids()
    if not chat_ids:
        return token, [], {"ok": False, "error": "AVITO_TG_CHAT_ID/AVITO_TG_CHAT_IDS is not set"}
    return token, chat_ids, None


def send_telegram_if_configured(record: NewListingRecord, settings: dict[str, object]) -> dict[str, object] | None:
    return send_listing_plain_if_configured(record, settings)


def send_listing_link_if_configured(
    *,
    title: str | None,
    price: int | None,
    url: str,
    settings: dict[str, object],
) -> dict[str, object] | None:
    token, chat_ids, error = _telegram_credentials(settings)
    if error is not None:
        return error
    if not token:
        return None
    results = [
        send_telegram_message(
            token=token,
            chat_id=chat_id,
            text=format_listing_link_message(title=title, price=price, url=url),
        )
        for chat_id in chat_ids
    ]
    return summarize_chat_results(results)


def send_listing_plain_if_configured(record: NewListingRecord, settings: dict[str, object]) -> dict[str, object] | None:
    token, chat_ids, error = _telegram_credentials(settings)
    if error is not None:
        return error
    if not token:
        return None

    chats: list[dict[str, object]] = []
    text = format_listing_plain_message(record)
    image_urls = listing_image_urls(record)
    for chat_id in chat_ids:
        if image_urls:
            sent = send_telegram_photos(
                token=token,
                chat_id=chat_id,
                image_urls=image_urls,
                caption=telegram_caption_text(text),
            )
            chat_result: dict[str, object] = {"chat_id": chat_id, "sent_as": "photo_caption", **sent}
            if not sent.get("ok"):
                fallback = send_telegram_message(token=token, chat_id=chat_id, text=text)
                chat_result["photo_send_error"] = sent.get("error") or sent
                chat_result["fallback_text"] = fallback
                if fallback.get("ok"):
                    chat_result["ok"] = True
                    chat_result["sent_as"] = "text_after_photo_error"
        else:
            sent = send_telegram_message(token=token, chat_id=chat_id, text=text)
            chat_result = {"chat_id": chat_id, "sent_as": "text", **sent}
        chats.append(chat_result)
    return summarize_chat_results(chats)


def send_profit_evaluation_if_configured(
    evaluation: dict[str, object],
    settings: dict[str, object],
    *,
    reply_to: dict[str, object] | None = None,
) -> dict[str, object] | None:
    token, chat_ids, error = _telegram_credentials(settings)
    if error is not None:
        return error
    if not token:
        return None

    reply_map = reply_message_map(reply_to)
    results = []
    for chat_id in chat_ids:
        results.append(
            {
                "chat_id": chat_id,
                **send_telegram_message(
                    token=token,
                    chat_id=chat_id,
                    text=format_profit_evaluation_message(evaluation),
                    reply_to_message_id=reply_map.get(chat_id),
                ),
            }
    )
    return summarize_chat_results(results)


def record_listing_id(record: NewListingRecord) -> str | None:
    for attr in ("bot_listing_id", "external_id"):
        value = getattr(record, attr, None)
        if value:
            return str(value)
    url = getattr(record, "url", None)
    if not url:
        return None
    match = re.search(r"_(\d{6,})(?:[/?#]|$)", str(url))
    if match:
        return match.group(1)
    match = re.search(r"/(\d{6,})(?:[/?#]|$)", str(url))
    return match.group(1) if match else None


def evaluation_listing_id(evaluation: dict[str, object]) -> str | None:
    for key in ("bot_listing_id", "external_id"):
        value = evaluation.get(key)
        if value:
            return str(value)
    listing = evaluation.get("listing")
    if isinstance(listing, dict):
        for key in ("bot_listing_id", "external_id"):
            value = listing.get(key)
            if value:
                return str(value)
    return None


def format_listing_plain_message(record: NewListingRecord) -> str:
    lines: list[str] = []
    listing_id = record_listing_id(record)
    if listing_id:
        lines.append(f"<b>#{html.escape(listing_id)}</b>")
    title_value = listing_display_value(record, "title")
    lines.append(f"<b>{html.escape(title_value)}</b>")
    lines.append(f"Цена: <b>{html.escape(listing_price_display(record))}</b>")
    posted_text = getattr(record, "posted_at_text", None)
    lines.append(f"Дата публикации: {html.escape(str(posted_text or 'не нашёл'))}")
    lines.append(f"Адрес: {html.escape(listing_address_display(record))}")
    lines.append(f"Доставка: {html.escape(listing_delivery_display(record))}")
    if record.description:
        lines.extend(["", html.escape(record.description)])
    if record.url:
        safe_url = html.escape(record.url)
        lines.extend(["", f'<a href="{safe_url}">Открыть объявление</a>'])
    return "\n".join(lines) or "Новое объявление"


def format_profit_evaluation_message(evaluation: dict[str, object]) -> str:
    total = evaluation.get("total") if isinstance(evaluation.get("total"), dict) else {}
    items = evaluation.get("items") if isinstance(evaluation.get("items"), list) else []
    expected_profit = value_from_dict(total, "expected_profit")
    expected_sell_total = value_from_dict(total, "expected_sell_total")
    expected_net_total = value_from_dict(total, "expected_net_total")
    buy_total = value_from_dict(total, "buy_total")
    lines = [f"Цена покупки: <b>{html.escape(money_text(buy_total, signed=False))}</b>"]

    listing_id = evaluation_listing_id(evaluation)
    if listing_id:
        lines.insert(0, f"<b>#{html.escape(listing_id)}</b>")
    visible_items = compact_cost_items_for_telegram(items)
    lines.append("")
    if len(visible_items) > 20:
        lines.append(f"Товаров слишком много: {len(visible_items)}. Лучше открыть объявление руками.")
    elif visible_items:
        lines.append("Товары:")
        lines.extend(f"{name} — {price}" for name, price in visible_items)
    else:
        lines.append("Товары: не распознаны")
    risks = evaluation.get("risks") if isinstance(evaluation.get("risks"), list) else []
    if has_unpriced_items(items, risks):
        lines.append("Профит не учитывает позиции «непонятно».")

    lines.extend(
        [
            "",
            f"Цена продажи: <b>{html.escape(money_text(expected_sell_total, signed=False))}</b>",
            f"Профит: <b>{html.escape(money_text(expected_profit))}</b>",
        ]
    )
    return "\n".join(lines)


def calculation_line(
    expected_sell_total: object,
    expected_net_total: object,
    buy_total: object,
    expected_profit: object,
) -> str:
    sale = numeric_value(expected_sell_total)
    net = numeric_value(expected_net_total)
    rate_text = "0.87"
    if sale and net is not None:
        rate_text = f"{net / sale:.2f}"
    return (
        f"Расчёт: {html.escape(money_text(expected_sell_total, signed=False))} × {rate_text} = "
        f"{html.escape(money_text(expected_net_total, signed=False))}; "
        f"{html.escape(money_text(expected_net_total, signed=False))} − "
        f"{html.escape(money_text(buy_total, signed=False))} = {html.escape(money_text(expected_profit))}"
    )


def numeric_value(value: object) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def listing_display_value(record: NewListingRecord, key: str) -> str:
    statuses = getattr(record, "listing_field_statuses", None)
    if isinstance(statuses, dict):
        value = statuses.get(key)
        if value is not None:
            return str(value)
    value = getattr(record, key, None)
    return str(value) if value not in (None, "") else "не нашёл"


def listing_price_display(record: NewListingRecord) -> str:
    if record.price is not None:
        return money_text(record.price, signed=False)
    return listing_display_value(record, "price")


def listing_address_display(record: NewListingRecord) -> str:
    statuses = getattr(record, "listing_field_statuses", None)
    if isinstance(statuses, dict) and statuses.get("address"):
        return str(statuses["address"])
    return clean_listing_address(record.address) or "не нашёл"


def listing_delivery_display(record: NewListingRecord) -> str:
    statuses = getattr(record, "listing_field_statuses", None)
    if isinstance(statuses, dict) and statuses.get("delivery"):
        return str(statuses["delivery"])
    delivery_price = clean_delivery_price(record)
    if delivery_price is not None:
        return f"от {delivery_price} ₽"
    status = getattr(record, "delivery_status", None)
    if status == "delivery_not_available":
        return "нет"
    if status == "delivery_available_price_unknown":
        return "есть, цена не найдена"
    return "не нашёл"


def clean_listing_address(address: str | None) -> str | None:
    if not address:
        return None
    lines = [re.sub(r"\s+", " ", line).strip() for line in str(address).replace("\xa0", " ").splitlines()]
    text = " ".join(line for line in lines if line).strip()
    if not text:
        return None
    text = re.sub(r"^во всех регионах\s*", "", text, flags=re.IGNORECASE).strip(" ,")
    if not text or text.lower() == "во всех регионах":
        return None
    return text


def clean_delivery_price(record: NewListingRecord) -> int | None:
    value = getattr(record, "delivery_price_rub", None)
    try:
        price = int(value)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def compact_cost_items_for_telegram(items: object) -> list[tuple[str, str]]:
    if not isinstance(items, list):
        return []
    result: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(
            item.get("canonical_name")
            or item.get("matched_catalog_name")
            or item.get("input_name")
            or item.get("name")
            or ""
        ).strip()
        if not name:
            continue
        quantity = item.get("quantity")
        try:
            quantity_int = int(quantity)
        except (TypeError, ValueError):
            quantity_int = 1
        if quantity_int > 1:
            name = f"{name} x{quantity_int}"
        buy_price = (
            item.get("buy_price_used")
            if item.get("buy_price_used") is not None
            else item.get("allocated_buy_price")
            if item.get("allocated_buy_price") is not None
            else item.get("buy_price")
        )
        if buy_price is None:
            price_text = "непонятно"
        else:
            price_text = money_text(buy_price, signed=False)
        result.append((html.escape(name), html.escape(price_text)))
    return result


def has_unpriced_items(items: object, risks: object) -> bool:
    if isinstance(items, list) and any(
        isinstance(item, dict) and item.get("buy_price_used") is None
        for item in items
    ):
        return True
    if isinstance(risks, list) and any(
        str(risk).startswith("no_price_for:") or risk == "profit_excludes_unpriced_items"
        for risk in risks
    ):
        return True
    return False


def value_from_dict(payload: object, key: str) -> object:
    if isinstance(payload, dict):
        return payload.get(key)
    return None


def money_text(value: object, *, signed: bool = True) -> str:
    if value is None:
        return "не посчитан"
    try:
        amount = round(float(value))
    except (TypeError, ValueError):
        return str(value)
    sign = "+" if signed and amount > 0 else ""
    return f"{sign}{amount} ₽"


def format_profit_items(items: object, *, suppress_item_profit: bool = False) -> list[str]:
    if not isinstance(items, list):
        return []
    lines: list[str] = []
    for item in items[:10]:
        if not isinstance(item, dict):
            continue
        input_name = str(item.get("input_name") or item.get("name") or "")
        canonical_name = str(item.get("canonical_name") or item.get("matched_catalog_name") or item.get("name") or "")
        if input_name and canonical_name and normalize_label(input_name) != normalize_label(canonical_name):
            name = f"{input_name} → {canonical_name}"
        else:
            name = canonical_name or input_name or "товар"
        profit = item.get("expected_profit")
        sell = item.get("expected_sell_price")
        reason = str(item.get("reason") or "").strip()
        confidence = str(item.get("confidence") or "").strip().lower()
        parts: list[str] = []
        if sell is not None:
            parts.append(f"продажа ~{money_text(sell, signed=False)}")
        if profit is not None and not suppress_item_profit:
            parts.append(f"профит {money_text(profit)}")
        elif profit is not None and suppress_item_profit:
            parts.append("профит по позиции не считаю: цена общая за лот")
        if confidence in {"low", "none"}:
            parts.append("уверенность низкая")
        if not parts:
            parts.append("в комплекте, отдельно не оценено")
        line_prefix = "•" if suppress_item_profit else ("✅" if profit is not None and sell is not None else "⚠️")
        line = f"{line_prefix} {name}: {', '.join(parts)}"
        if reason:
            clean_reason = translate_reason(reason)
            if clean_reason:
                line += f" — {clean_reason}"
        lines.append(html.escape(line))
    return lines


def human_decision_reasons(
    *,
    decision: str,
    expected_profit: object,
    risks: object,
    items: object,
    human_reason: str,
) -> list[str]:
    lines: list[str] = []
    try:
        profit_value = float(expected_profit) if expected_profit is not None else None
    except (TypeError, ValueError):
        profit_value = None
    if decision == "SKIP" and profit_value is not None and profit_value < 0:
        lines.append(f"— общий расчёт уходит в минус: {money_text(profit_value)}")
    elif decision == "GOOD" and profit_value is not None and profit_value > 0:
        lines.append(f"— расчёт показывает положительный профит: {money_text(profit_value)}")
    elif decision == "CHECK":
        lines.append("— есть шанс на сделку, но нужно вручную проверить спорные места")

    if isinstance(items, list):
        unpriced = [
            str(item.get("input_name") or item.get("name") or "")
            for item in items
            if isinstance(item, dict) and item.get("expected_sell_price") is None
        ]
        if unpriced:
            sample = ", ".join(name for name in unpriced[:4] if name)
            if sample:
                lines.append(f"— не удалось оценить: {sample}")

    translated_risks = [translate_risk(str(item)) for item in risks if item] if isinstance(risks, list) else []
    if any("доставка" in item for item in translated_risks):
        lines.append("— доставка не найдена или не учтена")
    if not lines and human_reason:
        lines.append(f"— {human_reason}")
    return [html.escape(line) for line in lines[:5]]


def translate_reason(reason: str) -> str:
    lowered = reason.lower()
    if "цена не найдена" in lowered or "not found" in lowered:
        return "нет цены в CRM/наблюдениях"
    if "market_observations" in lowered or "рыночное объявление" in lowered:
        return "цена взята из рыночных наблюдений"
    if "manual" in lowered or "bundle bonus" in lowered:
        return "ручное правило/бонус комплекта"
    return reason


def translate_risk(risk: str) -> str:
    if risk == "delivery_unknown":
        return "доставка неизвестна"
    if risk == "lot_price_distributed":
        return "цена одна за весь комплект; персональный профит по позициям не показывается"
    if risk == "listing_price_missing":
        return "цена объявления не найдена"
    if risk == "no_extracted_items":
        return "LLM не нашла товаров"
    if risk == "digital_product":
        return "есть признаки цифрового товара/аккаунта"
    if risk == "account_subscription_bundle_low_confidence_bonus":
        return "подписка/аккаунт учтены только маленьким бонусом, нужна ручная проверка"
    if risk.startswith("no_price_for:"):
        return f"нет цены в CRM для: {risk.split(':', 1)[1]}"
    return risk.replace("_", " ")


def normalize_label(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def format_listing_link_message(*, title: str | None, price: int | None, url: str) -> str:
    safe_title = html.escape(title or "Новое объявление")
    price_text = f"{price} ₽" if price is not None else "цена не найдена"
    safe_url = html.escape(url)
    return "\n".join(
        [
            "🆕 Новое объявление",
            f"<b>{safe_title}</b>",
            f"Цена: <b>{html.escape(price_text)}</b>",
            f'<a href="{safe_url}">Открыть объявление</a>',
            safe_url,
        ]
    )


def send_telegram_message(
    *,
    token: str,
    chat_id: str,
    text: str,
    reply_to_message_id: int | None = None,
    disable_web_page_preview: bool = True,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "chat_id": chat_id,
        "text": text[:3900],
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_web_page_preview,
    }
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
        payload["allow_sending_without_reply"] = True
    return telegram_api_request(token, "sendMessage", payload)


def send_telegram_photos(
    *,
    token: str,
    chat_id: str,
    image_urls: list[str],
    reply_to_message_id: int | None = None,
    caption: str | None = None,
) -> dict[str, object]:
    if not image_urls:
        return {"ok": True, "skipped": True}
    downloaded = download_telegram_images(image_urls)
    if not downloaded:
        return {"ok": False, "error": "no_images_downloaded"}
    if len(downloaded) == 1:
        payload: dict[str, object] = {
            "chat_id": chat_id,
        }
        if caption:
            payload["caption"] = caption
            payload["parse_mode"] = "HTML"
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
            payload["allow_sending_without_reply"] = True
        return telegram_multipart_api_request(
            token,
            "sendPhoto",
            fields=payload,
            files=[("photo", downloaded[0]["filename"], downloaded[0]["content_type"], downloaded[0]["content"])],
        )
    all_results: list[dict[str, object]] = []
    for batch_start in range(0, len(downloaded), 10):
        batch = downloaded[batch_start : batch_start + 10]
        media = [{"type": "photo", "media": f"attach://photo{index}"} for index, _ in enumerate(batch)]
        if caption and batch_start == 0:
            media[0]["caption"] = caption
            media[0]["parse_mode"] = "HTML"
        payload: dict[str, object] = {"chat_id": chat_id, "media": media}
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
            payload["allow_sending_without_reply"] = True
        files = [
            (f"photo{index}", item["filename"], item["content_type"], item["content"])
            for index, item in enumerate(batch)
        ]
        all_results.append(telegram_multipart_api_request(token, "sendMediaGroup", fields=payload, files=files))
    return {"ok": all(bool(item.get("ok")) for item in all_results), "batches": all_results}


def download_telegram_images(image_urls: list[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, url in enumerate(image_urls[:10]):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
                    ),
                    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                    "Referer": "https://www.avito.ru/",
                },
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                content = response.read()
                content_type = response.headers.get("Content-Type") or "image/jpeg"
        except OSError:
            continue
        if not content or len(content) > 9_000_000:
            continue
        if not str(content_type).lower().startswith("image/"):
            content_type = "image/jpeg"
        result.append(
            {
                "filename": f"avito_photo_{index}.jpg",
                "content_type": content_type,
                "content": content,
            }
        )
    return result


def telegram_caption_text(text: str) -> str:
    """Telegram photo/media captions are limited; keep full listing when possible."""
    if len(text) <= 1000:
        return text
    link_match = re.search(r'\n\n<a href="[^"]+">[^<]+</a>\s*$', text)
    link = link_match.group(0) if link_match else ""
    body = text[: max(0, 980 - len(link))].rstrip()
    return f"{body}...\n{link.strip()}" if link else f"{body}..."


def telegram_api_request(token: str, method: str, payload: dict[str, object]) -> dict[str, object]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response_body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        return {"ok": False, "error": f"HTTP {error.code}: {detail[:500]}"}
    except OSError as error:
        return {"ok": False, "error": str(error)}
    try:
        loaded = json.loads(response_body)
        return {"ok": bool(loaded.get("ok")), "response": loaded}
    except json.JSONDecodeError:
        return {"ok": False, "error": "Bad Telegram response", "raw": response_body[:500]}


def telegram_multipart_api_request(
    token: str,
    method: str,
    *,
    fields: dict[str, object],
    files: list[tuple[str, str, str, bytes]],
) -> dict[str, object]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    boundary = f"----codex-avito-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                json.dumps(value, ensure_ascii=False).encode("utf-8") if isinstance(value, (dict, list)) else str(value).encode("utf-8"),
                b"\r\n",
            ]
        )
    for field_name, filename, content_type, content in files:
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                (
                    f'Content-Disposition: form-data; name="{field_name}"; '
                    f'filename="{filename}"\r\n'
                ).encode("utf-8"),
                f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
                content,
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(chunks)
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            response_body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        return {"ok": False, "error": f"HTTP {error.code}: {detail[:500]}"}
    except OSError as error:
        return {"ok": False, "error": str(error)}
    try:
        loaded = json.loads(response_body)
        return {"ok": bool(loaded.get("ok")), "response": loaded}
    except json.JSONDecodeError:
        return {"ok": False, "error": "Bad Telegram response", "raw": response_body[:500]}


def summarize_chat_results(results: list[dict[str, object]]) -> dict[str, object]:
    return {"ok": all(bool(item.get("ok")) for item in results), "chats": results}


def telegram_message_id(result: dict[str, object]) -> int | None:
    direct_value = result.get("message_id")
    if isinstance(direct_value, int):
        return direct_value
    response = result.get("response")
    if isinstance(response, dict):
        message = response.get("result")
        if isinstance(message, dict):
            value = message.get("message_id")
            return int(value) if isinstance(value, int) else None
        if isinstance(message, list):
            for item in message:
                if isinstance(item, dict) and isinstance(item.get("message_id"), int):
                    return int(item["message_id"])
    batches = result.get("batches")
    if isinstance(batches, list):
        for batch in batches:
            if isinstance(batch, dict):
                value = telegram_message_id(batch)
                if value is not None:
                    return value
    return None


def reply_message_map(result: dict[str, object] | None) -> dict[str, int]:
    if not isinstance(result, dict):
        return {}
    chats = result.get("chats")
    if not isinstance(chats, list):
        return {}
    mapping: dict[str, int] = {}
    for item in chats:
        if not isinstance(item, dict):
            continue
        chat_id = str(item.get("chat_id") or "").strip()
        message_id = telegram_message_id(item)
        if chat_id and message_id:
            mapping[chat_id] = message_id
    return mapping


def listing_image_urls(record: NewListingRecord) -> list[str]:
    values: list[Any] = []
    for attr in ("image_urls", "images", "photo_urls", "photos"):
        value = getattr(record, attr, None)
        if isinstance(value, list):
            values.extend(value)
        elif isinstance(value, str):
            values.append(value)

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
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


def format_listing_message(record: NewListingRecord) -> str:
    parts = [
        format_listing_plain_message(record),
        "",
        f"<b>Профит:</b> {html.escape(metric_profit_text(record.profit_estimate))}",
        f"<b>⭐ Скор:</b> {html.escape(metric_score_text(record))}",
    ]
    if record.analysis:
        parts.extend(format_analysis(record.analysis))
    if record.profit_estimate:
        parts.extend(format_profit_estimate(record.profit_estimate))
    return "\n".join(parts)


def format_analysis(analysis: dict[str, object]) -> list[str]:
    verdict = label_value(analysis.get("verdict"), VERDICT_LABELS, "непонятно")
    interest = label_value(analysis.get("interest_label"), INTEREST_LABELS, "не указано")
    effective_price = analysis.get("effective_price_rub")
    price_basis = label_value(analysis.get("price_basis"), PRICE_BASIS_LABELS, "непонятно")
    delivery_available = label_value(analysis.get("delivery_available"), INTEREST_LABELS, "непонятно")
    delivery_terms = html.escape(str(analysis.get("delivery_terms") or ""))
    notes_value = analysis.get("notes", "")
    if analysis.get("ok") is False and not notes_value:
        notes_value = analysis.get("error", "")
    notes = html.escape(str(notes_value))
    errors_text = label_errors(analysis.get("errors", []))

    lines = [
        "",
        f"<b>Вердикт:</b> {html.escape(verdict)}",
        f"<b>Интерес:</b> {html.escape(interest)}",
        f"<b>Цена для оценки:</b> {html.escape(str(effective_price)) if effective_price is not None else 'не найдена'} ({html.escape(price_basis)})",
        f"<b>Доставка LLM:</b> {html.escape(delivery_available)}{(' — ' + delivery_terms) if delivery_terms else ''}",
        f"<b>Ошибки:</b> {html.escape(errors_text)}",
    ]
    if notes:
        lines.append(f"<b>Комментарий:</b> {notes}")
    return lines


def format_profit_estimate(estimate: dict[str, object]) -> list[str]:
    matches = estimate.get("matched_games", [])
    if isinstance(matches, list) and matches:
        games_text = ", ".join(
            f"{item.get('name')} ({item.get('price')} ₽)" for item in matches[:8] if isinstance(item, dict)
        )
    else:
        games_text = "не найдены в прайсе"
    profit = estimate.get("estimated_profit_rub")
    scenario = label_value(estimate.get("scenario"), SCENARIO_LABELS, "непонятно")
    profit_interest = label_value(estimate.get("is_interesting_by_profit"), INTEREST_LABELS, "непонятно")
    lines = [
        "",
        f"<b>Сценарий:</b> {html.escape(scenario)}",
        f"<b>Интерес по профиту:</b> {html.escape(profit_interest)}",
        f"<b>Игры из прайса:</b> {html.escape(games_text)}",
        f"<b>Сумма по прайсу:</b> {html.escape(str(estimate.get('resale_total_rub') or 0))} ₽",
        f"<b>Профит:</b> {html.escape(money_text(profit))}",
    ]
    lines.extend(format_quick_sell_estimate(estimate))
    lines.extend(format_lot_estimate(estimate))
    unknown_line = format_unknown_discs_line(estimate)
    if unknown_line:
        lines.append(unknown_line)
    review_reason = estimate.get("manual_review_reason")
    if review_reason:
        lines.append(f"<b>Надо посмотреть:</b> {html.escape(str(review_reason))}")
    if estimate.get("net_console_price_rub") is not None:
        lines.append(
            f"<b>Чистая цена приставки:</b> {html.escape(str(estimate.get('net_console_price_rub')))} ₽ "
            f"/ лимит {html.escape(str(estimate.get('console_limit_rub') or 'н/д'))} ₽"
        )
    return lines


def format_quick_sell_estimate(estimate: dict[str, object]) -> list[str]:
    if not estimate.get("quick_sell_rules_applied"):
        return []
    decision = label_value(estimate.get("decision"), DECISION_LABELS, "проверь")
    reason = html.escape(str(estimate.get("decision_reason") or ""))
    risks = estimate.get("risks")
    risk_text = ", ".join(str(item) for item in risks) if isinstance(risks, list) else str(risks or "нет")
    return [
        f"<b>Решение:</b> {html.escape(decision)}{(' — ' + reason) if reason else ''}",
        f"<b>Быстрая продажа:</b> {html.escape(str(estimate.get('quick_sell_price_rub') or 'н/д'))} ₽",
        f"<b>Макс. покупка:</b> {html.escape(str(estimate.get('max_buy_price_rub') or 'н/д'))} ₽",
        f"<b>Покупка с доставкой:</b> {html.escape(str(estimate.get('total_buy_price_rub') or 'н/д'))} ₽",
        f"<b>Комиссия/расход:</b> {html.escape(str(estimate.get('sale_commission_amount_rub') or 0))} ₽ + {html.escape(str(estimate.get('fixed_cost_per_disc_rub') or 0))} ₽",
        f"<b>Риски:</b> {html.escape(risk_text or 'нет')}",
    ]


def format_lot_estimate(estimate: dict[str, object]) -> list[str]:
    listing_type = str(estimate.get("listing_type") or estimate.get("scenario") or "")
    if not listing_type.startswith("game_lot") and listing_type != "digital_account":
        return []
    decision = label_value(estimate.get("decision") or estimate.get("lot_profit_decision"), DECISION_LABELS, "проверь")
    reason = html.escape(str(estimate.get("decision_reason") or estimate.get("notes") or ""))
    risks = estimate.get("lot_risks") or estimate.get("risks")
    risk_text = ", ".join(str(item) for item in risks) if isinstance(risks, list) else str(risks or "нет")
    lines = [
        f"<b>Решение по лоту:</b> {html.escape(decision)}{(' — ' + reason) if reason else ''}",
        f"<b>Игры в лоте:</b> {html.escape(str(estimate.get('lot_known_games_count') or 0))} найдено / {html.escape(str(estimate.get('lot_unknown_games_count') or 0))} неизвестно",
    ]
    if estimate.get("lot_quick_sell_sum") is not None:
        lines.append(f"<b>Быстрая продажа лота:</b> {html.escape(str(estimate.get('lot_quick_sell_sum')))} ₽")
    if estimate.get("lot_expected_profit") is not None:
        lines.append(f"<b>Профит лота:</b> {html.escape(str(estimate.get('lot_expected_profit')))} ₽")
    if estimate.get("lot_bundle_total_price") is not None:
        lines.append(
            f"<b>Комплектом:</b> {html.escape(str(estimate.get('lot_bundle_total_price')))} ₽"
            f" / поштучно {html.escape(str(estimate.get('lot_individual_price_sum') or 'н/д'))} ₽"
            f" / скидка {html.escape(str(estimate.get('lot_bundle_discount_rub') or 0))} ₽"
        )
    if estimate.get("lot_bundle_expected_profit") is not None:
        lines.append(f"<b>Профит комплектом:</b> {html.escape(str(estimate.get('lot_bundle_expected_profit')))} ₽")
    lines.append(f"<b>Риски лота:</b> {html.escape(risk_text or 'нет')}")
    items = estimate.get("lot_items")
    if isinstance(items, list) and items:
        item_lines = []
        for item in items[:6]:
            if not isinstance(item, dict):
                continue
            name = item.get("normalized_game_name") or item.get("raw_name") or "unknown"
            item_price = item.get("item_price")
            profit = item.get("expected_profit")
            decision_item = item.get("decision") or ""
            tail = []
            if item_price is not None:
                tail.append(f"цена {item_price}")
            if profit is not None:
                tail.append(f"профит {profit}")
            if decision_item:
                tail.append(str(decision_item))
            item_lines.append(f"{name} ({', '.join(tail)})" if tail else str(name))
        if item_lines:
            lines.append(f"<b>Позиции:</b> {html.escape('; '.join(item_lines))}")
    return lines


def metric_profit_text(estimate: dict[str, object] | None) -> str:
    if not estimate:
        return "не посчитан"
    profit = estimate.get("estimated_profit_rub")
    if profit is None:
        return "не посчитан"
    prefix = "примерно " if estimate.get("needs_manual_review") else ""
    return f"{prefix}{money_text(profit)}"


def format_unknown_discs_line(estimate: dict[str, object]) -> str | None:
    count = estimate.get("unknown_disc_count")
    avg = estimate.get("unknown_disc_avg_price_rub")
    total = estimate.get("unknown_disc_total_rub")
    if isinstance(count, int) and count > 0:
        return f"<b>Неизвестные диски:</b> {count} x {html.escape(str(avg or 700))} ₽ = {html.escape(str(total or 0))} ₽"
    if count == "unknown":
        return "<b>Неизвестные диски:</b> количество и названия не распознаны"
    return None


def metric_score_text(record: NewListingRecord) -> str:
    score = extract_existing_score(record.analysis) or extract_existing_score(record.profit_estimate)
    if score is None:
        full_text = "\n".join(
            [
                record.title or "",
                record.description or "",
                "\n".join(record.raw_detail_texts or []),
            ]
        )
        score = float(heuristic_condition_flags(full_text)["score"])
    if score <= 1:
        score = 1 + score * 4
    score = max(1.0, min(5.0, float(score)))
    rounded = round(score, 1)
    if rounded.is_integer():
        return f"{int(rounded)}/5"
    return f"{rounded}/5"


def extract_existing_score(payload: dict[str, object] | None) -> float | None:
    if not isinstance(payload, dict):
        return None
    for key in ("condition_score_5", "score_5", "condition_score"):
        value = payload.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.replace(",", "."))
            except ValueError:
                pass
    return None


def label_item_kind(value: object) -> str:
    return label_value(
        value,
        {
            "game": "игра/диск",
            "console": "приставка",
            "accessory": "аксессуар",
            "mixed": "смешанный лот",
            "other": "другое",
            "unknown": "непонятно",
        },
        "непонятно",
    )


def label_value(value: object, mapping: dict[str, str], default: str) -> str:
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    return mapping.get(text, text)


def label_errors(errors: object) -> str:
    labels = {
        "title_description_mismatch": "название и описание расходятся",
        "incomplete_description": "описание может быть неполным",
        "price_mismatch": "цена в карточке и описании расходится",
        "suspicious_digital": "похоже на цифровой товар/аккаунт",
        "llm_http_error": "ошибка запроса к LLM",
        "llm_not_configured": "LLM не настроена",
        "other": "другая ошибка",
    }
    if isinstance(errors, list):
        return ", ".join(labels.get(str(item), str(item)) for item in errors) or "нет"
    if errors:
        return labels.get(str(errors), str(errors))
    return "нет"
