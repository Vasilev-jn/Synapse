from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class ListingDetails:
    address: str | None
    description: str | None
    url: str | None
    raw_texts: list[str]


@dataclass
class NewListingRecord:
    saved_at: str
    fingerprint: str
    title: str | None
    price: int | None
    address: str | None
    description: str | None
    url: str | None
    raw_card_texts: list[str]
    raw_detail_texts: list[str]
    delivery_text: str | None = None
    delivery_price_rub: int | None = None
    delivery_status: str | None = None
    seller_city: str | None = None
    item_kind: str | None = None
    analysis: dict[str, object] | None = None
    profit_estimate: dict[str, object] | None = None


def load_seen(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    if isinstance(payload, list):
        return {str(item) for item in payload}
    return set()


def save_seen(path: Path, seen: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8")


def append_record(record: NewListingRecord, jsonl_path: Path, latest_path: Path) -> None:
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with jsonl_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    latest_path.write_text(json.dumps(asdict(record), ensure_ascii=False, indent=2), encoding="utf-8")


def max_cards_per_cycle(settings: dict[str, object]) -> int:
    pipeline = settings.get("pipeline", {})
    if not isinstance(pipeline, dict):
        return 2
    return int(pipeline.get("max_cards_per_cycle", 2))


def title_is_rental(title: str | None) -> bool:
    if not title:
        return False
    lowered = title.lower().replace("ё", "е")
    return bool(re.search(r"\b(аренд\w*|прокат\w*)\b", lowered))


def seller_city_from_address(address: str | None) -> str | None:
    if not address:
        return None
    for line in address.splitlines():
        candidate = line.strip()
        if candidate:
            return candidate.split(",")[0].strip() or None
    return None


def item_kind_from_title(title: str | None) -> str:
    lowered = (title or "").lower().replace("ё", "е")
    game_signals = ("диск", "диски", "игра", "игры", "game", "games")
    console_signals = (
        "пристав",
        "консол",
        "playstation 3 superslim",
        "playstation 4 slim",
        "playstation 4 pro",
        "playstation 4 fat",
        "playstation 4 phat",
        "playstation 5 slim",
        "playstation 5 pro",
        "ps4 slim",
        "ps4 pro",
        "ps4 fat",
        "ps4 phat",
        "ps5 slim",
        "ps5 pro",
        "ps 4 slim",
        "ps 4 pro",
        "ps 4 fat",
        "ps 4 phat",
        "sony ps",
        "500gb",
        "500 гб",
        "1tb",
        "1 тб",
    )
    if any(signal in lowered for signal in console_signals) and not any(signal in lowered for signal in game_signals):
        return "console"
    if any(signal in lowered for signal in game_signals):
        return "game"
    return "unknown"


def delivery_text_from_texts(texts: list[str]) -> str | None:
    candidates: list[str] = []
    for text in texts:
        cleaned = re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()
        lowered = cleaned.lower().replace("ё", "е")
        if "достав" not in lowered and "отправ" not in lowered:
            continue
        if "?" in cleaned and lowered.startswith(("отправите", "сможете отправить", "авито доставкой сможете")):
            continue
        if lowered.startswith(("здравствуйте", "добрый", "готов", "интересует")):
            continue
        candidates.append(cleaned)
    priced = [text for text in candidates if re.search(r"\bот\s+\d[\d\s]*\s*(?:₽|руб|р\b)", text, re.IGNORECASE)]
    return (priced or candidates or [None])[0]


def delivery_price_from_text(text: str | None) -> int | None:
    if not text:
        return None
    normalized = str(text).replace("\xa0", " ")
    prices: list[int] = []
    for match in re.finditer(r"(\d[\d\s]*)\s*(?:₽|руб|р\b)", normalized, re.IGNORECASE):
        if delivery_price_match_is_noise(normalized, match.start(), match.end()):
            continue
        try:
            prices.append(int(match.group(1).replace(" ", "")))
        except ValueError:
            continue
    return min(prices) if prices else None


def delivery_price_match_is_noise(text: str, start: int, end: int) -> bool:
    """Ignore installment/credit prices rendered near Avito delivery widgets."""

    window = text[max(0, start - 180) : min(len(text), end + 180)].lower().replace("ё", "е")
    return any(
        marker in window
        for marker in (
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
    )


def delivery_status_from_text(text: str | None) -> str:
    if not text:
        return "delivery_not_found"
    lowered = text.lower().replace("ё", "е").replace("\xa0", " ")
    if any(marker in lowered for marker in ("доставки нет", "нет доставки", "без доставки", "только самовывоз")):
        return "delivery_not_available"
    if delivery_price_from_text(text) is not None:
        return "delivery_available_price_found"
    return "delivery_available_price_unknown"


def max_scrolls_inside_listing(settings: dict[str, object]) -> int:
    pipeline = settings.get("pipeline", {})
    if not isinstance(pipeline, dict):
        return 4
    return int(pipeline.get("max_scrolls_inside_listing", 4))
