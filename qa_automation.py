"""Сбор карточек из выдачи Авито через вручную запущенный AdsPower."""

from __future__ import annotations

import json
import argparse
import html as html_lib
import os
import random
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright


ROOT_DIR = Path(__file__).resolve().parent
LOCAL_SETTINGS_FILE = ROOT_DIR / "monitor_local_settings.json"
PROFILE_ID = "k1favsy2"
ADSPOWER_CACHE_DIR = Path(r"G:\.ADSPOWER_GLOBAL\cache")
TARGET_URL = (
    "https://www.avito.ru/all/igry_pristavki_i_programmy/"
    "igry_pristavki_i_programmy/igrovye_pristavki_i_aksessuary/"
    "igrovye_pristavki-ASgBAgICAkSSAsoJ9M0UmsqPAw"
    "?context=H4sIAAAAAAAA_wEmANn_YToxOntzOjE6InkiO3M6MTY6IlVSRllEUjZBWlF5UnVzSlgiO31GW7RWJgAAAA"
    "&q=ps4&s=104"
)
CRM_SIGHTINGS_URL = "http://127.0.0.1:8001/api/market/sightings/avito"

OUTPUT_DIR = ROOT_DIR / "json_responses"
ITEMS_OUTPUT_DIR = OUTPUT_DIR / "avito_items"
SNAPSHOTS_OUTPUT_DIR = OUTPUT_DIR / "page_snapshots"
SEEN_URLS_FILE = OUTPUT_DIR / "seen_avito_urls.json"
FAILED_URLS_FILE = OUTPUT_DIR / "failed_avito_urls.json"
STATUS_FILE = OUTPUT_DIR / "monitor_status.json"
EVENTS_LOG_FILE = OUTPUT_DIR / "monitor_events.jsonl"
CONTROL_STOP_FILE = OUTPUT_DIR / "monitor_stop.flag"
MONITOR_BASE_PAUSE_SECONDS = 35
MONITOR_RANDOM_PAUSE_MIN_SECONDS = 1
MONITOR_RANDOM_PAUSE_MAX_SECONDS = 20
FAILED_RETRY_DELAY_SECONDS = 5 * 60
MAX_NEW_ITEMS_PER_CYCLE = 10
MAX_OPEN_PAGES_TO_KEEP = 2
MONITOR_MAX_RUNTIME_SECONDS = int(2.5 * 60 * 60)
MIN_PAUSE_MS = 300
MAX_PAUSE_MS = 900

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
ROTATION_WAIT_SECONDS = 10
MAX_BLOCK_RETRIES = 2


def monitor_stop_requested() -> bool:
    return CONTROL_STOP_FILE.exists()


def load_rotation_urls() -> list[str]:
    """Читает ссылки ротации из env или локального не-git файла."""
    raw = os.environ.get("AVITO_PROXY_ROTATION_URLS", "")
    if raw.strip():
        return [
            value.strip()
            for value in re.split(r"[\n;,]+", raw)
            if value.strip()
        ]
    if LOCAL_SETTINGS_FILE.exists():
        try:
            data = json.loads(LOCAL_SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        values = data.get("rotation_urls") if isinstance(data, dict) else None
        if isinstance(values, list):
            return [str(value).strip() for value in values if str(value).strip()]
    return []


ROTATION_URLS = load_rotation_urls()


def find_cdp_endpoint() -> str:
    """Находит CDP WebSocket вручную запущенного профиля AdsPower."""
    candidates = list(
        ADSPOWER_CACHE_DIR.glob(f"{PROFILE_ID}_*/DevToolsActivePort")
    )
    if not candidates:
        raise RuntimeError(
            f"DevToolsActivePort для профиля {PROFILE_ID} не найден. "
            "Сначала вручную запустите профиль AdsPower."
        )

    errors: list[str] = []
    for devtools_file in sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True):
        try:
            lines = devtools_file.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            errors.append(f"{devtools_file}: {error}")
            continue
        if not lines:
            errors.append(f"{devtools_file}: пустой DevToolsActivePort")
            continue
        port = lines[0].strip()
        if not port.isdigit():
            errors.append(f"{devtools_file}: некорректный порт {port!r}")
            continue
        endpoint = f"http://127.0.0.1:{port}"
        try:
            with urllib.request.urlopen(f"{endpoint}/json/version", timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
        except Exception as error:
            errors.append(f"{endpoint}: /json/version не ответил: {error}")
            continue
        if payload.get("webSocketDebuggerUrl"):
            return endpoint
        errors.append(f"{endpoint}: нет webSocketDebuggerUrl в /json/version")

    raise RuntimeError(
        "Не нашёл живой CDP endpoint AdsPower. "
        "Откройте профиль AdsPower вручную и попробуйте снова. "
        f"Проверенные варианты: {'; '.join(errors[-5:])}"
    )


def safe_filename(value: str) -> str:
    """Создаёт безопасное имя файла."""
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "_", value)
    return normalized.strip("_.")[:120] or "item"


def write_json(data: Any, output_path: Path) -> None:
    """Безопасно записывает JSON, создавая родительские каталоги."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


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


def min_delivery_price_from_text(text: str | None) -> int | None:
    if not text:
        return None
    normalized = html_lib.unescape(str(text)).replace("\xa0", " ")
    prices: list[int] = []
    patterns = (
        r"(?:достав\w*|delivery|shipping)[^₽рубР]{0,180}?(\d[\d\s]{1,8})\s*(?:₽|руб\.?|р\b)",
        r"(?:от|from)\s*(\d[\d\s]{1,8})\s*(?:₽|руб\.?|р\b)[^.\n]{0,120}(?:достав\w*|delivery|shipping)",
        r'"(?:deliveryPrice|deliveryCost|shippingCost|minDeliveryPrice|minDeliveryCost)"\s*:\s*(\d{2,6})',
        r'\\"(?:deliveryPrice|deliveryCost|shippingCost|minDeliveryPrice|minDeliveryCost)\\"\s*:\s*(\d{2,6})',
    )
    for pattern in patterns:
        for match in re.finditer(pattern, normalized, flags=re.IGNORECASE):
            if delivery_price_match_is_noise(normalized, match.start(), match.end()):
                continue
            try:
                price = int(match.group(1).replace(" ", ""))
            except ValueError:
                continue
            if 1 <= price <= 20_000:
                prices.append(price)
    return min(prices) if prices else None


def delivery_price_match_is_noise(text: str, start: int, end: int) -> bool:
    """Filter non-delivery prices that often sit near the delivery block.

    Avito can render an installments widget directly after "Купить с доставкой".
    Without this guard, the parser may read "2 131 ₽ × 10 месяцев" as delivery.
    """

    window = text[max(0, start - 180) : min(len(text), end + 180)].lower().replace("ё", "е")
    bad_markers = (
        "рассроч",
        "платеж",
        "платёж",
        "месяц",
        "месяцев",
        "мес.",
        "взнос",
        "переплат",
        "кредит",
        "installment",
        "installments",
    )
    if any(marker in window for marker in bad_markers):
        return True

    # Promo/cashback banners mention delivery but their numbers are not a
    # delivery tariff. A real tariff normally has "доставка от N ₽".
    if any(marker in window for marker in ("кешбэк", "cashback", "альфа-банк", "альфа банк")):
        exact_delivery_price = re.search(
            r"(?:достав\w*|delivery|shipping)[^.\n]{0,80}?(?:от|from)\s*\d[\d\s]{0,8}\s*(?:₽|руб\.?|р\b)",
            window,
            flags=re.IGNORECASE,
        )
        if not exact_delivery_price:
            return True
    return False


def delivery_text_from_html(html_text: str) -> str | None:
    if not html_text:
        return None
    plain = re.sub(r"<[^>]+>", " ", html_text)
    plain = html_lib.unescape(plain).replace("\xa0", " ")
    snippets: list[str] = []
    for marker in ("достав", "delivery", "shipping", "самовывоз"):
        for match in re.finditer(marker, plain, flags=re.IGNORECASE):
            start = max(0, match.start() - 120)
            end = min(len(plain), match.end() + 260)
            snippet = re.sub(r"\s+", " ", plain[start:end]).strip()
            if snippet and snippet not in snippets:
                snippets.append(snippet)
            if len(snippets) >= 8:
                break
        if snippets:
            break
    priced = [snippet for snippet in snippets if min_delivery_price_from_text(snippet) is not None]
    return (priced or snippets or [None])[0]


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


def extract_avito_state_from_html(html_text: str, collected_at: str | None = None) -> dict[str, Any]:
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


def save_page_snapshot(page: Page, item_id: str) -> dict[str, str | None]:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot_dir = SNAPSHOTS_OUTPUT_DIR / safe_filename(item_id)
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    html_path = snapshot_dir / f"{timestamp}.html"
    result: dict[str, str | None] = {
        "screenshot": None,
        "html": str(html_path),
    }

    try:
        html_path.write_text(page.content(), encoding="utf-8")
    except Exception as error:
        result["html"] = None
        print(f"[SNAPSHOT] Не удалось сохранить HTML: {error}")

    return result


def load_seen_urls() -> set[str]:
    if not SEEN_URLS_FILE.exists():
        return set()
    try:
        data = json.loads(SEEN_URLS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    if not isinstance(data, list):
        return set()
    return {str(url) for url in data if url}


def save_seen_urls(seen_urls: set[str]) -> None:
    write_json(sorted(seen_urls), SEEN_URLS_FILE)


def load_failed_urls() -> dict[str, dict[str, Any]]:
    if not FAILED_URLS_FILE.exists():
        return {}
    try:
        data = json.loads(FAILED_URLS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save_failed_urls(failed_urls: dict[str, dict[str, Any]]) -> None:
    write_json(failed_urls, FAILED_URLS_FILE)


def mark_failed_url(
    failed_urls: dict[str, dict[str, Any]] | None,
    url: str,
    reason: str,
) -> None:
    if failed_urls is None or not url:
        return
    now = time.time()
    entry = failed_urls.get(url, {})
    failed_urls[url] = {
        "fail_count": int(entry.get("fail_count", 0)) + 1,
        "last_error": reason[:1_000],
        "last_failed_at": datetime.now().isoformat(timespec="seconds"),
        "retry_after": now + FAILED_RETRY_DELAY_SECONDS,
    }
    save_failed_urls(failed_urls)
    log_event(
        "item_failed",
        url=url,
        reason=reason[:1_000],
        fail_count=failed_urls[url]["fail_count"],
    )


def clear_failed_url(
    failed_urls: dict[str, dict[str, Any]] | None,
    url: str,
) -> None:
    if failed_urls is None:
        return
    if url in failed_urls:
        failed_urls.pop(url, None)
        save_failed_urls(failed_urls)


def is_failed_url_ready(
    failed_urls: dict[str, dict[str, Any]] | None,
    url: str,
) -> bool:
    if failed_urls is None or url not in failed_urls:
        return True
    return time.time() >= float(failed_urls[url].get("retry_after", 0))


def write_monitor_status(**status: Any) -> None:
    status.setdefault("updated_at", datetime.now().isoformat(timespec="seconds"))
    write_json(status, STATUS_FILE)


def post_catalog_sightings(
    catalog_items: list[dict[str, Any]],
    *,
    source_url: str,
    cycle_number: int | None,
    cycle_started_at: str | None,
    bootstrap_all: bool,
) -> None:
    if not catalog_items:
        return
    sightings = []
    for position, item in enumerate(catalog_items, start=1):
        url = str(item.get("url") or "")
        sightings.append(
            {
                "external_id": item_id_from_url(url, position),
                "url": url,
                "title": item.get("title") or item.get("name"),
                "price": item.get("price"),
                "cycle_number": cycle_number,
                "bootstrap_all": bootstrap_all,
                "catalog_position": position,
                "source_url": source_url,
            }
        )
    payload = {
        "observed_at": datetime.now().isoformat(timespec="seconds"),
        "cycle_number": cycle_number,
        "cycle_started_at": cycle_started_at,
        "bootstrap_all": bootstrap_all,
        "source_url": source_url,
        "sightings": sightings,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        CRM_SIGHTINGS_URL,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
        log_event("catalog_sightings_sent", count=len(sightings), cycle_number=cycle_number)
    except Exception as error:
        log_event("catalog_sightings_failed", count=len(sightings), error=str(error)[:500])


def log_event(event: str, **payload: Any) -> None:
    EVENTS_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "event": event,
        **payload,
    }
    with EVENTS_LOG_FILE.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")


def next_monitor_pause_seconds() -> int:
    return MONITOR_BASE_PAUSE_SECONDS + random.randint(
        MONITOR_RANDOM_PAUSE_MIN_SECONDS,
        MONITOR_RANDOM_PAUSE_MAX_SECONDS,
    )


def seed_seen_urls_from_saved_items(seen_urls: set[str]) -> set[str]:
    for item_file in ITEMS_OUTPUT_DIR.glob("*.json"):
        try:
            data = json.loads(item_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        url = data.get("requested_url") or data.get("final_url")
        if url:
            seen_urls.add(str(url))
            seen_urls.add(seen_key_for_url(str(url)))
    return seen_urls


def rotate_proxy_ip() -> None:
    """Запрашивает смену IP мобильного прокси напрямую."""
    if not ROTATION_URLS:
        print("[PROXY] Ссылки ротации не заданы, пропускаем смену IP")
        log_event("proxy_rotation_skipped", reason="rotation_urls_not_configured")
        return
    print("[PROXY] Запрашиваем ротацию IP")
    log_event("proxy_rotation_start")
    errors: list[str] = []
    for index, rotation_url in enumerate(ROTATION_URLS, start=1):
        try:
            request = urllib.request.Request(
                rotation_url,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status != 200:
                    raise RuntimeError(
                        f"Сервис ротации вернул HTTP {response.status}"
                    )
                body = response.read().decode("utf-8", errors="replace")
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError:
                    payload = None
                if isinstance(payload, dict) and payload.get("status") == "err":
                    raise RuntimeError(
                        f"Ротация отклонена: {payload.get('message', body)}"
                    )
            log_event("proxy_rotation_url_ok", url_index=index)
            break
        except Exception as error:
            errors.append(f"{index}: {error}")
            log_event("proxy_rotation_url_error", url_index=index, error=str(error))
    else:
        raise RuntimeError("Все ссылки ротации упали: " + "; ".join(errors))
    print(f"[PROXY] IP сменён, ждём {ROTATION_WAIT_SECONDS} секунд")
    log_event("proxy_rotation_done", wait_seconds=ROTATION_WAIT_SECONDS)
    time.sleep(ROTATION_WAIT_SECONDS)


def cleanup_extra_pages(
    context,
    keep_pages: int = MAX_OPEN_PAGES_TO_KEEP,
    protected_pages: tuple[Page, ...] = (),
) -> None:
    pages = list(context.pages)
    for page in pages[keep_pages:]:
        if page in protected_pages:
            continue
        try:
            if not page.is_closed():
                page.close()
        except Exception:
            continue


def wait_for_catalog_data(page: Page) -> None:
    """Ждёт появления предложений каталога в JSON-LD."""
    page.wait_for_function(
        """() => [...document.querySelectorAll(
            'script[type="application/ld+json"]'
        )].some(element => element.textContent.includes('"offers"'))""",
        timeout=15_000,
    )


def collect_catalog_items(page: Page) -> list[dict[str, Any]]:
    """Извлекает карточки выдачи из JSON-LD."""
    wait_for_catalog_data(page)
    catalog_items: list[dict[str, Any]] = []

    for raw_json in page.locator(
        'script[type="application/ld+json"]'
    ).all_text_contents():
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError:
            continue

        graph = data.get("@graph", []) if isinstance(data, dict) else []
        for node in graph:
            if not isinstance(node, dict) or node.get("@type") != "Product":
                continue

            offers = node.get("offers", {})
            nested_offers = (
                offers.get("offers", []) if isinstance(offers, dict) else []
            )
            if isinstance(nested_offers, list):
                catalog_items.extend(
                    item for item in nested_offers if isinstance(item, dict)
                )

    # Убираем дубликаты, сохраняя порядок выдачи.
    unique_items: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for item in catalog_items:
        url = str(item.get("url", ""))
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique_items.append(item)

    return unique_items


def text_by_marker(page: Page, marker: str) -> str | None:
    """Возвращает текст первого найденного data-marker."""
    locator = page.locator(f'[data-marker="{marker}"]').first
    try:
        if locator.count() and locator.is_visible():
            value = locator.inner_text().strip()
            return value or None
    except Exception:
        pass
    return None


def text_by_marker_fragments(
    page: Page,
    fragments: tuple[str, ...],
) -> str | None:
    """Ищет видимый текст по части data-marker."""
    values: list[str] = []
    for fragment in fragments:
        locator = page.locator(f'[data-marker*="{fragment}"]')
        for index in range(min(locator.count(), 10)):
            try:
                element = locator.nth(index)
                if element.is_visible():
                    value = element.inner_text(timeout=1_000).strip()
                    if value and value not in values:
                        values.append(value)
            except Exception:
                continue
    return "\n\n".join(values) or None


def text_by_selector(page: Page, selector: str) -> str | None:
    """Возвращает видимый текст первого найденного CSS-селектора."""
    locator = page.locator(selector).first
    try:
        if locator.count() and locator.is_visible():
            value = locator.inner_text(timeout=2_000).strip()
            return value or None
    except Exception:
        pass
    return None


def prepare_item_page(page: Page) -> None:
    """Дожидается карточки, прокручивает её и раскрывает описание."""
    try:
        page.wait_for_selector(
            '[data-marker="item-view/title-info"]',
            state="attached",
            timeout=15_000,
        )
    except Exception:
        pass

    for _ in range(4):
        page.mouse.wheel(0, random.randint(600, 900))
        page.wait_for_timeout(random.randint(250, 450))

    description = page.locator(
        '[data-marker="item-view/item-description"]'
    ).first
    if description.count():
        controls = description.locator('button, [role="button"]')
        for index in range(controls.count()):
            try:
                control = controls.nth(index)
                label = control.inner_text(timeout=500).strip().lower()
                if control.is_visible() and any(
                    text in label
                    for text in ("показать полностью", "ещё", "далее")
                ):
                    control.click(timeout=2_000)
                    page.wait_for_timeout(500)
                    break
            except Exception:
                continue


def optional_attribute(
    page: Page,
    selector: str,
    attribute: str,
) -> str | None:
    """Читает необязательный атрибут без длительного ожидания."""
    locator = page.locator(selector).first
    try:
        if locator.count():
            return locator.get_attribute(attribute, timeout=2_000)
    except Exception:
        pass
    return None


def parse_json_ld(page: Page) -> list[Any]:
    """Читает все корректные JSON-LD блоки страницы."""
    result: list[Any] = []
    for raw_json in page.locator(
        'script[type="application/ld+json"]'
    ).all_text_contents():
        try:
            result.append(json.loads(raw_json))
        except json.JSONDecodeError:
            continue
    return result


def is_avito_listing_image_url(url: str) -> bool:
    """Return True only for Avito item/gallery photos, not banners or service images."""
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


def collect_image_urls(page: Page, catalog_item: dict[str, Any], json_ld: list[Any]) -> list[str]:
    """Collect Avito listing image URLs without downloading/analyzing images."""
    candidates: list[Any] = []
    image = catalog_item.get("image")
    if isinstance(image, list):
        candidates.extend(image)
    elif isinstance(image, str):
        candidates.append(image)

    for node in json_ld:
        if not isinstance(node, dict):
            continue
        graph = node.get("@graph") if isinstance(node.get("@graph"), list) else [node]
        for item in graph:
            if not isinstance(item, dict):
                continue
            node_image = item.get("image")
            if isinstance(node_image, list):
                candidates.extend(node_image)
            elif isinstance(node_image, str):
                candidates.append(node_image)

    try:
        dom_images = page.eval_on_selector_all(
            (
                '[data-marker="image-preview/preview-image"], '
                '[data-marker*="image-preview"] img, '
                '[data-marker*="item-view/gallery"] img, '
                '[data-marker*="item-view/photo"] img'
            ),
            """elements => elements
                .map(img => {
                    const srcset = img.getAttribute('srcset') || '';
                    let bestUrl = '';
                    let bestScore = -1;
                    for (const part of srcset.split(',')) {
                        const pieces = part.trim().split(/\\s+/);
                        const url = pieces[0];
                        if (!url) continue;
                        const descriptor = pieces[1] || '1x';
                        const score = Number.parseFloat(descriptor) || 1;
                        if (score >= bestScore) {
                            bestScore = score;
                            bestUrl = url;
                        }
                    }
                    return bestUrl || img.currentSrc || img.src || img.getAttribute('src');
                })
                .filter(Boolean)
            """,
        )
        if isinstance(dom_images, list):
            candidates.extend(dom_images)
    except Exception:
        pass

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


def detect_access_block(page: Page) -> str | None:
    """Определяет CAPTCHA или ограничение доступа."""
    title = page.title().lower()
    body_start = page.locator("body").inner_text(timeout=5_000)[:2_000].lower()
    indicators = (
        "доступ ограничен",
        "подтвердите, что вы не робот",
        "captcha",
        "слишком много запросов",
    )
    return next(
        (indicator for indicator in indicators if indicator in title or indicator in body_start),
        None,
    )


def handle_item_interstitials(page: Page) -> None:
    """Handles Avito item interstitials that can appear before the real card."""
    age_texts = (
        "18+",
        "мне есть 18",
        "мне исполнилось 18",
        "да, есть",
        "есть",
        "продолжить",
        "показать объявление",
    )
    captcha_texts = (
        "подтвердите, что вы не робот",
        "captcha",
        "капча",
    )

    for _ in range(3):
        try:
            body_text = page.locator("body").inner_text(timeout=5_000).lower()
        except Exception:
            body_text = ""

        if page.locator('[data-marker="item-view/title-info"]').count():
            return

        if any(text in body_text for text in captcha_texts):
            print("[ITEM] Найдена капча, жду ручного решения в открытой вкладке")
            page.wait_for_selector(
                '[data-marker="item-view/title-info"]',
                timeout=180_000,
            )
            return

        if any(text in body_text for text in age_texts):
            controls = page.locator('button, [role="button"], a')
            for index in range(min(controls.count(), 30)):
                try:
                    control = controls.nth(index)
                    label = control.inner_text(timeout=500).strip().lower()
                    if not control.is_visible():
                        continue
                    if any(
                        text in label
                        for text in (
                            "мне есть 18",
                            "мне исполнилось 18",
                            "да, есть",
                            "есть",
                            "продолжить",
                            "показать объявление",
                            "да",
                        )
                    ):
                        print(f"[ITEM] Нажимаю промежуточную кнопку: {label}")
                        control.click(timeout=5_000)
                        page.wait_for_timeout(3_000)
                        break
                except Exception:
                    continue
        else:
            return


def collect_item_details(
    page: Page,
    catalog_item: dict[str, Any],
    position: int,
) -> dict[str, Any]:
    """Собирает доступные структурированные и видимые данные карточки."""
    canonical = optional_attribute(
        page,
        'link[rel="canonical"]',
        "href",
    )
    description_meta = optional_attribute(
        page,
        'meta[name="description"]',
        "content",
    )

    collected_at = datetime.now().isoformat(timespec="seconds")
    try:
        html_text = page.content()
    except Exception:
        html_text = ""
    json_ld = parse_json_ld(page)
    image_urls = collect_image_urls(page, catalog_item, json_ld)
    visible_delivery = (
        text_by_marker(page, "item-view/delivery")
        or text_by_marker_fragments(page, ("delivery",))
    )
    html_delivery_text = delivery_text_from_html(html_text)
    delivery_price_rub = (
        min_delivery_price_from_text(visible_delivery)
        or min_delivery_price_from_text(html_delivery_text)
        or min_delivery_price_from_text(html_text)
    )
    if delivery_price_rub and (not visible_delivery or "достав" in (html_delivery_text or "").lower() or "delivery" in (html_delivery_text or "").lower()):
        delivery_text = f"{visible_delivery or html_delivery_text or 'Доставка'}\nМинимальная доставка: от {delivery_price_rub} ₽"
    else:
        delivery_text = visible_delivery or html_delivery_text

    return {
        "position": position,
        "collected_at": collected_at,
        "requested_url": catalog_item.get("url"),
        "final_url": page.url,
        "canonical_url": canonical,
        "page_title": page.title(),
        "meta_description": description_meta,
        "catalog_summary": {
            key: value
            for key, value in catalog_item.items()
            if key != "image"
        },
        "visible": {
            "title": text_by_marker(page, "item-view/title-info"),
            "price": text_by_marker(page, "item-view/item-price"),
            "description": text_by_marker(page, "item-view/item-description"),
            "parameters": text_by_marker(page, "item-view/item-params"),
            "address": (
                text_by_selector(page, '#item-view-address [itemprop="address"]')
                or text_by_marker(page, "item-view/item-address")
                or text_by_marker_fragments(
                    page,
                    ("address", "location", "geo"),
                )
            ),
            "seller": text_by_marker_fragments(page, ("seller",)),
            "delivery": delivery_text,
        },
        "delivery_price_rub": delivery_price_rub,
        "avito_state": extract_avito_state_from_html(html_text, collected_at),
        "image_urls": image_urls,
        "json_ld": json_ld,
    }


def item_id_from_url(url: str, position: int) -> str:
    """Извлекает числовой ID объявления из конца URL."""
    match = re.search(r"_(\d+)(?:[/?#]|$)", url)
    return match.group(1) if match else f"position_{position:03d}"


def seen_key_for_url(url: str) -> str:
    """Stable de-dup key: prefer Avito listing id over full URL/query string."""
    item_id = item_id_from_url(url, 0)
    return item_id if item_id and not item_id.startswith("position_") else str(url)


def catalog_item_seen(item: dict[str, Any], seen_urls: set[str] | None) -> bool:
    if seen_urls is None:
        return False
    url = str(item.get("url", ""))
    return bool(url and (url in seen_urls or seen_key_for_url(url) in seen_urls))


def process_catalog(
    context,
    seen_urls: set[str] | None = None,
    failed_urls: dict[str, dict[str, Any]] | None = None,
    catalog_page: Page | None = None,
    bootstrap_all: bool = False,
    cycle_number: int | None = None,
    cycle_started_at: str | None = None,
) -> list[dict[str, Any]]:
    """Открывает новую выдачу и последовательно обходит её карточки."""
    own_catalog_page = catalog_page is None
    if catalog_page is None or catalog_page.is_closed():
        catalog_page = context.new_page()
    catalog_items: list[dict[str, Any]] = []

    try:
        print("[CATALOG] Открываем новую выдачу")
        for catalog_attempt in range(MAX_BLOCK_RETRIES + 1):
            try:
                if "avito.ru" in catalog_page.url and catalog_page.url != "about:blank":
                    print("[CATALOG] action=reload existing catalog tab")
                    catalog_page.reload(
                        wait_until="domcontentloaded",
                        timeout=60_000,
                    )
                else:
                    print("[CATALOG] action=goto target url")
                    catalog_page.goto(
                        TARGET_URL,
                        wait_until="domcontentloaded",
                        timeout=60_000,
                    )
                catalog_page.wait_for_timeout(2_000)
                block_reason = detect_access_block(catalog_page)
            except Exception as error:
                block_reason = f"catalog_open_failed: {error}"
            if not block_reason:
                try:
                    catalog_items = collect_catalog_items(catalog_page)
                    if catalog_items:
                        break
                    block_reason = "catalog_has_no_items"
                except Exception as error:
                    block_reason = f"catalog_data_not_found: {error}"
            if catalog_attempt == MAX_BLOCK_RETRIES:
                log_event(
                    "catalog_block_failed",
                    reason=block_reason,
                    attempt=catalog_attempt + 1,
                )
                raise RuntimeError(
                    f"Выдача заблокирована после повторов: {block_reason}"
                )
            print(f"[CATALOG] Ограничение: {block_reason}")
            log_event(
                "catalog_block",
                reason=block_reason,
                attempt=catalog_attempt + 1,
            )
            rotate_proxy_ip()

        print(f"[CATALOG] Найдено карточек: {len(catalog_items)}")
        if monitor_stop_requested():
            log_event("monitor_stop_requested_inside_catalog", stage="after_catalog_load")
            return []
        log_event(
            "catalog_loaded",
            source_url=catalog_page.url,
            found_count=len(catalog_items),
            bootstrap_all=bootstrap_all,
        )
        post_catalog_sightings(
            catalog_items,
            source_url=catalog_page.url,
            cycle_number=cycle_number,
            cycle_started_at=cycle_started_at,
            bootstrap_all=bootstrap_all,
        )
        if bootstrap_all:
            found_count = len(catalog_items)
            fresh_items = [
                item for item in catalog_items
                if not catalog_item_seen(item, seen_urls)
            ]
            print(
                f"[CATALOG] Первый цикл: берём верхние "
                f"{MAX_NEW_ITEMS_PER_CYCLE} объявлений"
            )
            print(f"[CATALOG] Already seen before bootstrap: {found_count - len(fresh_items)}")
            catalog_items = fresh_items[:MAX_NEW_ITEMS_PER_CYCLE]
            log_event(
                "catalog_filtered",
                found_count=found_count,
                fresh_count=len(fresh_items),
                delayed_count=0,
                taken_count=len(catalog_items),
                bootstrap_all=True,
            )
        elif seen_urls is not None:
            fresh_items = [
                item for item in catalog_items
                if not catalog_item_seen(item, seen_urls)
            ]
            new_catalog_items = [
                item for item in fresh_items
                if is_failed_url_ready(failed_urls, str(item.get("url", "")))
            ]
            delayed_count = len(fresh_items) - len(new_catalog_items)
            print(f"[CATALOG] Новых карточек всего: {len(fresh_items)}")
            print(f"[CATALOG] Отложено после ошибок: {delayed_count}")
            catalog_items = new_catalog_items[:MAX_NEW_ITEMS_PER_CYCLE]
            print(f"[CATALOG] Берём в этот цикл: {len(catalog_items)}")
            log_event(
                "catalog_filtered",
                found_count=len(catalog_items),
                fresh_count=len(fresh_items),
                delayed_count=delayed_count,
                taken_count=len(catalog_items),
                bootstrap_all=False,
            )

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        write_json(
            {
                "source_url": catalog_page.url,
                "count": len(catalog_items),
                "items": catalog_items,
            },
            OUTPUT_DIR / f"{timestamp}_avito_catalog.json",
        )
        log_event(
            "catalog_saved",
            timestamp=timestamp,
            count=len(catalog_items),
            file=str(OUTPUT_DIR / f"{timestamp}_avito_catalog.json"),
        )

        collected: list[dict[str, Any]] = []
        for position, catalog_item in enumerate(catalog_items, start=1):
            if monitor_stop_requested():
                log_event(
                    "monitor_stop_requested_inside_catalog",
                    stage="before_item",
                    position=position,
                    collected_count=len(collected),
                )
                break
            url = str(catalog_item.get("url", ""))
            item_id = item_id_from_url(url, position)
            bot_listing_id = seen_key_for_url(url)
            output_path = ITEMS_OUTPUT_DIR / f"{safe_filename(item_id)}.json"

            print(f"[ITEM {position}/{len(catalog_items)}] {url}")
            for attempt in range(MAX_BLOCK_RETRIES + 1):
                if monitor_stop_requested():
                    log_event(
                        "monitor_stop_requested_inside_item",
                        url=url,
                        item_id=item_id,
                        attempt=attempt + 1,
                    )
                    break
                # Каждая карточка открывается новой вкладкой того же профиля.
                item_page = context.new_page()
                try:
                    item_page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=60_000,
                    )
                    item_page.wait_for_timeout(random.randint(1_500, 3_000))
                    handle_item_interstitials(item_page)

                    block_reason = detect_access_block(item_page)
                    if block_reason:
                        log_event(
                            "item_block",
                            url=url,
                            reason=block_reason,
                            attempt=attempt + 1,
                        )
                        print(
                            f"[ITEM] Ограничение: {block_reason} "
                            f"(попытка {attempt + 1}/{MAX_BLOCK_RETRIES + 1})"
                        )
                        if attempt < MAX_BLOCK_RETRIES:
                            item_page.close()
                            rotate_proxy_ip()
                            continue
                        print("[ITEM] Карточка пропущена после повторов")
                        mark_failed_url(failed_urls, url, f"blocked: {block_reason}")
                        break

                    prepare_item_page(item_page)
                    details = collect_item_details(
                        item_page,
                        catalog_item,
                        position,
                    )
                    details["bot_listing_id"] = bot_listing_id
                    details["external_id"] = bot_listing_id
                    details["monitor_context"] = {
                        "cycle_number": cycle_number,
                        "cycle_started_at": cycle_started_at,
                        "bootstrap_all": bootstrap_all,
                        "catalog_source_url": catalog_page.url,
                        "catalog_position": position,
                        "catalog_found_count": len(catalog_items),
                        "seen_before": catalog_item_seen(catalog_item, seen_urls),
                    }
                    if not details["visible"]["title"] or not details["visible"]["price"]:
                        print(
                            f"[ITEM] Неполная загрузка "
                            f"(попытка {attempt + 1}/{MAX_BLOCK_RETRIES + 1})"
                        )
                        if attempt < MAX_BLOCK_RETRIES:
                            item_page.close()
                            continue
                        print("[ITEM] Карточка пропущена как неполная")
                        mark_failed_url(failed_urls, url, "incomplete_page")
                        break

                    details["snapshot"] = save_page_snapshot(item_page, item_id)
                    write_json(details, output_path)
                    try:
                        from app.pipeline import process_details

                        pipeline_result = process_details(
                            details,
                            source_path=output_path,
                            send_telegram=True,
                        )
                        print(
                            "[PIPELINE] "
                            f"crm={pipeline_result.get('crm_import_result', {}).get('ok')} "
                            f"profit={pipeline_result.get('crm_evaluation_result', {}).get('ok')} "
                            f"items={pipeline_result.get('llm_items_count')}"
                        )
                    except Exception as pipeline_error:
                        print(f"[PIPELINE] Ошибка интеграции: {pipeline_error}")
                        log_event(
                            "pipeline_failed",
                            url=url,
                            item_id=item_id,
                            error=str(pipeline_error)[:1_000],
                        )
                    collected.append(details)
                    log_event(
                        "item_saved",
                        url=url,
                        item_id=item_id,
                        title=details["visible"].get("title"),
                        output_path=str(output_path),
                        snapshot=details.get("snapshot"),
                    )
                    if seen_urls is not None:
                        seen_urls.add(url)
                        seen_urls.add(bot_listing_id)
                        save_seen_urls(seen_urls)
                    clear_failed_url(failed_urls, url)
                    print(f"[ITEM] Сохранено: {output_path.name}")
                    break
                except Exception as error:
                    print(f"[ITEM] Ошибка: {error}")
                    mark_failed_url(failed_urls, url, str(error))
                    break
                finally:
                    if not item_page.is_closed():
                        item_page.close()

            catalog_page.wait_for_timeout(
                random.randint(MIN_PAUSE_MS, MAX_PAUSE_MS)
            )

        write_json(
            {
                "source_url": catalog_page.url,
                "count": len(collected),
                "items": collected,
            },
            OUTPUT_DIR / f"{timestamp}_avito_item_details.json",
        )
        log_event(
            "details_saved",
            timestamp=timestamp,
            count=len(collected),
            file=str(OUTPUT_DIR / f"{timestamp}_avito_item_details.json"),
        )
        print(f"[DONE] Собрано подробных карточек: {len(collected)}")
        return collected
    finally:
        if own_catalog_page and not catalog_page.is_closed():
            catalog_page.close()


def run_qa(target_url: str | None = None, *, max_runtime_seconds: int | None = None) -> None:
    global TARGET_URL
    if target_url:
        TARGET_URL = target_url
    if CONTROL_STOP_FILE.exists():
        CONTROL_STOP_FILE.unlink()
    ws_endpoint = find_cdp_endpoint()
    write_monitor_status(status="connecting_adspower", cdp_endpoint=ws_endpoint)
    print(f"[ADSPOWER] Подключаемся к CDP: {ws_endpoint}", flush=True)
    seen_urls = seed_seen_urls_from_saved_items(load_seen_urls())
    failed_urls = load_failed_urls()
    save_seen_urls(seen_urls)

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.connect_over_cdp(ws_endpoint, timeout=15_000)
        except Exception as error:
            raise RuntimeError(
                "Не удалось подключиться к AdsPower. "
                f"CDP endpoint: {ws_endpoint}. "
                "Сначала вручную откройте профиль и повторите запуск."
            ) from error

        if not browser.contexts:
            raise RuntimeError("AdsPower не предоставил контекст браузера")

        write_monitor_status(status="running", cdp_endpoint=ws_endpoint)
        print("[ADSPOWER] Playwright подключён к открытому профилю")
        print(
            "[MONITOR] Постоянный режим: пауза 50 секунд "
            "+ случайно 1-30 секунд"
        )
        catalog_page = browser.contexts[0].new_page()
        cycle_number = 0
        monitor_started_at = time.time()
        bootstrap_completed = False
        runtime_limit = max_runtime_seconds or MONITOR_MAX_RUNTIME_SECONDS
        finish_status = "finished"
        finish_reason = "runtime_limit_reached"
        while time.time() - monitor_started_at < runtime_limit:
            if CONTROL_STOP_FILE.exists():
                log_event(
                    "monitor_stop_requested",
                    cycle_number=cycle_number,
                    seen_count=len(seen_urls),
                    failed_count=len(failed_urls),
                )
                write_monitor_status(
                    status="stopping",
                    cycle_number=cycle_number,
                    seen_count=len(seen_urls),
                    failed_count=len(failed_urls),
                )
                finish_status = "stopped"
                finish_reason = "stop_flag_requested"
                break
            cycle_number += 1
            cycle_started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            pause_seconds = next_monitor_pause_seconds()
            bootstrap_all = not bootstrap_completed
            print(f"[MONITOR] Новый цикл: {cycle_started_at}")
            log_event(
                "cycle_start",
                cycle_number=cycle_number,
                bootstrap_all=bootstrap_all,
            )
            cleanup_extra_pages(
                browser.contexts[0],
                protected_pages=(catalog_page,),
            )
            try:
                if catalog_page.is_closed():
                    catalog_page = browser.contexts[0].new_page()
                collected = process_catalog(
                    browser.contexts[0],
                    seen_urls,
                    failed_urls,
                    catalog_page,
                    bootstrap_all,
                    cycle_number=cycle_number,
                    cycle_started_at=cycle_started_at,
                )
                print(f"[MONITOR] За цикл сохранено новых карточек: {len(collected)}")
                if bootstrap_all:
                    bootstrap_completed = True
                log_event(
                    "cycle_done",
                    cycle_number=cycle_number,
                    bootstrap_all=bootstrap_all,
                    saved_in_cycle=len(collected),
                    seen_count=len(seen_urls),
                    failed_count=len(failed_urls),
                    next_run_after_seconds=pause_seconds,
                )
                write_monitor_status(
                    status="ok",
                    cycle_number=cycle_number,
                    bootstrap_all=bootstrap_all,
                    cycle_started_at=cycle_started_at,
                    saved_in_cycle=len(collected),
                    seen_count=len(seen_urls),
                    failed_count=len(failed_urls),
                    next_run_after_seconds=pause_seconds,
                )
            except Exception as error:
                print(f"[MONITOR] Ошибка цикла: {error}")
                log_event(
                    "cycle_error",
                    cycle_number=cycle_number,
                    bootstrap_all=bootstrap_all,
                    error=str(error),
                    seen_count=len(seen_urls),
                    failed_count=len(failed_urls),
                    next_run_after_seconds=pause_seconds,
                )
                write_monitor_status(
                    status="error",
                    cycle_number=cycle_number,
                    bootstrap_all=bootstrap_all,
                    cycle_started_at=cycle_started_at,
                    last_error=str(error),
                    seen_count=len(seen_urls),
                    failed_count=len(failed_urls),
                    next_run_after_seconds=pause_seconds,
                )
            print(f"[MONITOR] Пауза {pause_seconds} секунд")
            for _ in range(pause_seconds):
                if CONTROL_STOP_FILE.exists():
                    break
                time.sleep(1)
        log_event(
            "monitor_finished",
            cycle_number=cycle_number,
            runtime_seconds=int(time.time() - monitor_started_at),
            seen_count=len(seen_urls),
            failed_count=len(failed_urls),
            status=finish_status,
            reason=finish_reason,
        )
        write_monitor_status(
            status=finish_status,
            reason=finish_reason,
            cycle_number=cycle_number,
            seen_count=len(seen_urls),
            failed_count=len(failed_urls),
        )
        if finish_status == "stopped":
            print("[MONITOR] Остановлен по команде, завершаю работу")
        else:
            print("[MONITOR] Лимит времени достигнут, завершаю работу")
        # Профиль AdsPower не закрываем.


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Avito AdsPower monitor")
    parser.add_argument("--target-url", help="Avito search URL to monitor")
    parser.add_argument(
        "--max-runtime-seconds",
        type=int,
        default=None,
        help="Override monitor runtime limit",
    )
    args = parser.parse_args()
    try:
        run_qa(args.target_url, max_runtime_seconds=args.max_runtime_seconds)
    except Exception as error:
        write_monitor_status(
            status="failed",
            reason=str(error),
            last_error=str(error),
        )
        raise
