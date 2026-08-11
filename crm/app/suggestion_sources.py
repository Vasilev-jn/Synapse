"""Read-only autocomplete sources for local CRM forms."""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import ROOT_DIR


GAME_SUGGESTIONS_ZIP = ROOT_DIR / "crm_game_suggestions.zip"
GAME_SUGGESTIONS_JSON = "crm_game_suggestions.json"
PROFIT_KNOWLEDGE_ZIP = ROOT_DIR / "crm_profit_knowledge.zip"
PROFIT_KNOWLEDGE_JSON = "crm_profit_knowledge.json"


@dataclass(frozen=True)
class TitleSuggestion:
    label: str
    search: str
    category: str
    weight: int = 0


@dataclass(frozen=True)
class ProfitKnowledgeItem:
    id: str
    item_type: str
    canonical_name: str
    price_source: str
    price_confidence: str
    net_after_sale_rub: float | None
    good_net_after_sale_rub: float | None
    bad_net_after_sale_rub: float | None
    base_market_price_rub: float | None
    quick_sell_price_rub: float | None
    max_buy_price_rub: float | None
    sort_weight: int = 0


@dataclass(frozen=True)
class ProfitKnowledge:
    items_by_id: dict[str, ProfitKnowledgeItem]
    aliases: list[tuple[str, str, int]]


@lru_cache(maxsize=1)
def external_game_title_suggestions() -> tuple[TitleSuggestion, ...]:
    if not GAME_SUGGESTIONS_ZIP.exists():
        return ()
    try:
        with zipfile.ZipFile(GAME_SUGGESTIONS_ZIP) as archive:
            with archive.open(GAME_SUGGESTIONS_JSON) as handle:
                payload = json.load(handle)
    except (OSError, KeyError, zipfile.BadZipFile, json.JSONDecodeError):
        return ()

    items = payload.get("items", []) if isinstance(payload, dict) else []
    suggestions: list[TitleSuggestion] = []
    for raw_item in items:
        if not isinstance(raw_item, dict):
            continue
        label = str(raw_item.get("canonical_name") or "").strip()
        if not label:
            continue
        terms = [label]
        for key in ("search_terms", "aliases", "normalized_terms"):
            value = raw_item.get(key)
            if isinstance(value, list):
                terms.extend(str(term).strip() for term in value if str(term).strip())
        search = " ".join(dict.fromkeys(terms)).casefold()
        suggestions.append(
            TitleSuggestion(
                label=label,
                search=search,
                category="game",
                weight=_safe_int(raw_item.get("sort_weight")),
            )
        )
    return tuple(sorted(suggestions, key=lambda item: (-item.weight, item.label.casefold())))


@lru_cache(maxsize=1)
def profit_knowledge() -> ProfitKnowledge:
    if not PROFIT_KNOWLEDGE_ZIP.exists():
        return ProfitKnowledge(items_by_id={}, aliases=[])
    try:
        with zipfile.ZipFile(PROFIT_KNOWLEDGE_ZIP) as archive:
            with archive.open(PROFIT_KNOWLEDGE_JSON) as handle:
                payload = json.load(handle)
    except (OSError, KeyError, zipfile.BadZipFile, json.JSONDecodeError):
        return ProfitKnowledge(items_by_id={}, aliases=[])

    raw_items = payload.get("items", []) if isinstance(payload, dict) else []
    items_by_id: dict[str, ProfitKnowledgeItem] = {}
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        item_id = str(raw_item.get("id") or "").strip()
        name = str(raw_item.get("canonical_name") or "").strip()
        if not item_id or not name:
            continue
        sale_math = raw_item.get("sale_math") if isinstance(raw_item.get("sale_math"), dict) else {}
        good_math = sale_math.get("good_condition") if isinstance(sale_math.get("good_condition"), dict) else {}
        bad_math = sale_math.get("bad_condition") if isinstance(sale_math.get("bad_condition"), dict) else {}
        item = ProfitKnowledgeItem(
            id=item_id,
            item_type=str(raw_item.get("item_type") or "").strip(),
            canonical_name=name,
            price_source=str(raw_item.get("price_source") or "").strip(),
            price_confidence=str(raw_item.get("price_confidence") or "").strip(),
            net_after_sale_rub=_safe_float(sale_math.get("net_after_sale_rub")),
            good_net_after_sale_rub=_safe_float(good_math.get("net_after_sale_rub")),
            bad_net_after_sale_rub=_safe_float(bad_math.get("net_after_sale_rub")),
            base_market_price_rub=_safe_float(sale_math.get("base_market_price_rub") or good_math.get("base_market_price_rub")),
            quick_sell_price_rub=_safe_float(sale_math.get("quick_sell_price_rub") or good_math.get("quick_sell_price_rub")),
            max_buy_price_rub=_safe_float(sale_math.get("max_buy_price_rub") or good_math.get("max_buy_price_rub")),
            sort_weight=_safe_int(raw_item.get("sort_weight")),
        )
        items_by_id[item.id] = item

    aliases: list[tuple[str, str, int]] = []
    raw_aliases = payload.get("alias_index", []) if isinstance(payload, dict) else []
    for raw_alias in raw_aliases:
        if not isinstance(raw_alias, dict):
            continue
        item_id = str(raw_alias.get("item_id") or "").strip()
        if item_id not in items_by_id:
            continue
        normalized = normalize_lookup_text(str(raw_alias.get("normalized") or raw_alias.get("term") or ""))
        if normalized:
            aliases.append((normalized, item_id, _safe_int(raw_alias.get("weight"))))
    for item in items_by_id.values():
        aliases.append((normalize_lookup_text(item.canonical_name), item.id, item.sort_weight))

    deduped: dict[tuple[str, str], int] = {}
    for alias, item_id, weight in aliases:
        key = (alias, item_id)
        deduped[key] = max(weight, deduped.get(key, 0))
    sorted_aliases = sorted(
        ((alias, item_id, weight) for (alias, item_id), weight in deduped.items()),
        key=lambda row: (-row[2], -len(row[0]), row[0]),
    )
    return ProfitKnowledge(items_by_id=items_by_id, aliases=sorted_aliases)


def normalize_lookup_text(value: str) -> str:
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
    parts = []
    current = []
    for char in text:
        if char.isalnum():
            current.append(char)
        elif current:
            parts.append("".join(current))
            current = []
    if current:
        parts.append("".join(current))
    return " ".join(parts)


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
