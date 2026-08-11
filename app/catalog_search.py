from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "data" / "crm" / "crm_game_suggestions.json"
CRM_CATALOG_URL = "http://127.0.0.1:8001/api/catalog/game-suggestions"

_TOKEN_RE = re.compile(r"[0-9a-z\u0430-\u044f\u0451]+", re.IGNORECASE)
_NOISE_TOKENS = {
    "ps",
    "ps4",
    "ps5",
    "ps3",
    "playstation",
    "sony",
    "disk",
    "disc",
    "game",
    "games",
    "gb",
    "tb",
    "\u0438\u0433\u0440\u0430",
    "\u0438\u0433\u0440\u044b",
    "\u0438\u0433\u0440",
    "\u0434\u0438\u0441\u043a",
    "\u0434\u0438\u0441\u043a\u0438",
    "\u0434\u0438\u0441\u043a\u043e\u0432",
    "\u043f\u0440\u0438\u0441\u0442\u0430\u0432\u043a\u0430",
    "\u043a\u043e\u043d\u0441\u043e\u043b\u044c",
}

EXTRA_ALIASES: dict[str, list[str]] = {
    "spider_man": [
        "\u0447\u0435\u043b\u043e\u0432\u0435\u043a \u043f\u0430\u0443\u043a",
        "\u0447\u0435\u043b\u043e\u0432\u0435\u043a-\u043f\u0430\u0443\u043a",
        "\u0447\u0435\u043b\u043e\u0432\u0435\u043a\u0430-\u043f\u0430\u0443\u043a\u0430",
        "\u0447\u0435\u043b\u043e\u0432\u0435\u043a\u0430 \u043f\u0430\u0443\u043a\u0430",
        "\u0447\u0435\u043b\u043e\u0432\u0435\u043a \u043f\u0430\u0443\u043a \u043f\u0435\u0440\u0432\u0430\u044f \u0447\u0430\u0441\u0442\u044c",
        "\u043f\u0435\u0440\u0432\u044b\u0439 \u0447\u0435\u043b\u043e\u0432\u0435\u043a \u043f\u0430\u0443\u043a",
    ],
    "spider_man_miles_morales": [
        "\u043c\u0430\u0439\u043b\u0437 \u043c\u043e\u0440\u0430\u043b\u0435\u0441",
        "\u043c\u0430\u0439\u043b\u0441 \u043c\u043e\u0440\u0430\u043b\u0435\u0441",
        "\u043c\u043e\u0440\u0430\u043b\u0435\u0441",
    ],
    "it_takes_two": [
        "\u0438\u0442 \u0442\u0435\u0439\u043a \u0442\u0443",
        "\u0438\u0442 \u0442\u0435\u0439\u043a\u0441 \u0442\u0443",
        "\u0438\u0442\u0442\u0435\u0439\u043a\u0441\u0442\u0443",
    ],
    "gta_5": [
        "\u0433\u0442\u0430 5",
        "\u0433\u0442\u0430\u0035",
        "\u0433\u0442\u0430 v",
    ],
    "ufc": [
        "\u044e\u0444\u0441",
        "\u044e\u0444\u0441 4",
        "ufc 4",
    ],
    "mafia_trilogy": [
        "\u0442\u0440\u0438\u043b\u043e\u0433\u0438\u044f \u043c\u0430\u0444\u0438\u0438",
        "\u043c\u0430\u0444\u0438\u044f \u0442\u0440\u0438\u043b\u043e\u0433\u0438\u044f",
        "mafia trilogy",
    ],
    "god_of_war": [
        "\u0433\u043e\u0434 \u043e\u0444 \u0432\u0430\u0440",
        "\u0431\u043e\u0433 \u0432\u043e\u0439\u043d\u044b",
    ],
    "gran_turismo_sport": [
        "\u0433\u0440\u0430\u043d \u0442\u0443\u0440\u0438\u0437\u043c\u043e",
        "\u0433\u0440\u0430\u043d\u0442\u0443\u0440\u0438\u0437\u043c\u043e",
        "grand turismo",
    ],
    "dead_island_2": ["dead island 2", "dead island 2 ps4", "dead island 2 ps5"],
    "bioshock_the_collection": ["bioshock the collection", "bioshock collection", "биошок коллекция"],
    "castlevania_anniversary_collection": ["castlevania anniversary collection"],
    "warhammer_40000_space_marine_2": [
        "warhammer 40 000 space marine 2",
        "warhammer 40000 space marine 2",
        "space marine 2",
    ],
    "resident_evil_7_biohazard": ["resident evil biohazard", "resident evil 7", "re7 biohazard"],
    "fifa_22": ["fifa 22", "фифа 22"],
    "ea_sports_fc_25": ["fc 25", "fc25", "ea sports fc 25"],
    "little_nightmares_2": ["little nightmares 2", "little nightmares ii", "литл найтмерс 2"],
    "call_of_duty_black_ops_7": ["call of duty black ops 7", "black ops 7", "cod bo7"],
    "fishing_north_atlantic": ["fishing north atlantic"],
    "wrc_10": ["wrc 10", "wrc10"],
    "tom_clancys_rainbow_six_extraction": [
        "tom clancy rainbow эвакуация",
        "rainbow six extraction",
        "rainbow эвакуация",
    ],
    "playstation_portal": ["ps portal", "playstation portal", "playstation 5 portal"],
    "metal_gear_solid_v": ["metal gear solid v", "metal gear solid 5", "mgsv", "mgs v"],
    "mount_blade_2_bannerlord": ["mount blade 2", "mount and blade 2", "mount blade ii", "bannerlord"],
    "the_last_of_us_part_ii": ["одни из нас 2", "одни из нас part 2", "the last of us 2", "last of us 2"],
    "lego_marvel_avengers": ["lego мстители", "lego marvel мстители", "лего avengers"],
    "fifa_18": ["fifa 18", "fifa 18 ultimate team", "фифа 18"],
}

EXTRA_ITEMS: dict[str, dict[str, Any]] = {
    "dead_island_2": {
        "id": "dead_island_2",
        "canonical_name": "Dead Island 2",
        "platform": "ps4",
        "item_type": "game",
        "category": "manual",
        "confidence": "high",
        "sort_weight": 950,
    },
    "bioshock_the_collection": {
        "id": "bioshock_the_collection",
        "canonical_name": "BioShock: The Collection",
        "platform": "ps4",
        "item_type": "game",
        "category": "manual",
        "confidence": "high",
        "sort_weight": 950,
    },
    "castlevania_anniversary_collection": {
        "id": "castlevania_anniversary_collection",
        "canonical_name": "Castlevania Anniversary Collection",
        "platform": "ps4",
        "item_type": "game",
        "category": "manual",
        "confidence": "high",
        "sort_weight": 950,
    },
    "warhammer_40000_space_marine_2": {
        "id": "warhammer_40000_space_marine_2",
        "canonical_name": "Warhammer 40,000: Space Marine 2",
        "platform": "ps5",
        "item_type": "game",
        "category": "manual",
        "confidence": "high",
        "sort_weight": 950,
    },
    "resident_evil_7_biohazard": {
        "id": "resident_evil_7_biohazard",
        "canonical_name": "Resident Evil 7: Biohazard",
        "platform": "ps4",
        "item_type": "game",
        "category": "manual",
        "confidence": "high",
        "sort_weight": 950,
    },
    "fifa_22": {
        "id": "fifa_22",
        "canonical_name": "FIFA 22",
        "platform": "ps4",
        "item_type": "game",
        "category": "manual",
        "confidence": "high",
        "sort_weight": 950,
    },
    "ea_sports_fc_25": {
        "id": "ea_sports_fc_25",
        "canonical_name": "EA Sports FC 25",
        "platform": "ps4",
        "item_type": "game",
        "category": "manual",
        "confidence": "high",
        "sort_weight": 950,
    },
    "little_nightmares_2": {
        "id": "little_nightmares_2",
        "canonical_name": "Little Nightmares 2",
        "platform": "ps4",
        "item_type": "game",
        "category": "manual",
        "confidence": "high",
        "sort_weight": 950,
    },
    "call_of_duty_black_ops_7": {
        "id": "call_of_duty_black_ops_7",
        "canonical_name": "Call of Duty Black Ops 7",
        "platform": "ps4",
        "item_type": "game",
        "category": "manual",
        "confidence": "low",
        "sort_weight": 700,
    },
    "fishing_north_atlantic": {
        "id": "fishing_north_atlantic",
        "canonical_name": "Fishing: North Atlantic",
        "platform": "ps4",
        "item_type": "game",
        "category": "manual",
        "confidence": "high",
        "sort_weight": 850,
    },
    "wrc_10": {
        "id": "wrc_10",
        "canonical_name": "WRC 10",
        "platform": "ps4",
        "item_type": "game",
        "category": "manual",
        "confidence": "high",
        "sort_weight": 900,
    },
    "tom_clancys_rainbow_six_extraction": {
        "id": "tom_clancys_rainbow_six_extraction",
        "canonical_name": "Tom Clancy's Rainbow Six Extraction",
        "platform": "ps4",
        "item_type": "game",
        "category": "manual",
        "confidence": "high",
        "sort_weight": 900,
    },
    "playstation_portal": {
        "id": "playstation_portal",
        "canonical_name": "PlayStation Portal",
        "platform": "ps5",
        "item_type": "console",
        "category": "accessory",
        "confidence": "high",
        "sort_weight": 950,
    },
    "metal_gear_solid_v": {
        "id": "metal_gear_solid_v",
        "canonical_name": "Metal Gear Solid V",
        "platform": "ps4",
        "item_type": "game",
        "category": "manual",
        "confidence": "high",
        "sort_weight": 950,
    },
    "mount_blade_2_bannerlord": {
        "id": "mount_blade_2_bannerlord",
        "canonical_name": "Mount & Blade II: Bannerlord",
        "platform": "ps4",
        "item_type": "game",
        "category": "manual",
        "confidence": "high",
        "sort_weight": 900,
    },
    "fifa_18": {
        "id": "fifa_18",
        "canonical_name": "FIFA 18",
        "platform": "ps4",
        "item_type": "game",
        "category": "manual",
        "confidence": "high",
        "sort_weight": 900,
    },
}

EXTRA_ALIASES.update(
    {
        "heavy_rain_and_beyond_two_souls": [
            "heavy rain and beyond two souls",
            "heavy rain beyond two souls",
            "heavy rain & beyond two souls",
            "heavy rain + beyond two souls",
            "heavy rain and beyond: two souls",
            "heavy rain beyond: two souls",
            "\u0445\u0435\u0432\u0438 \u0440\u0435\u0439\u043d \u0438 \u0437\u0430 \u0433\u0440\u0430\u043d\u044c\u044e \u0434\u0432\u0435 \u0434\u0443\u0448\u0438",
            "\u0445\u0435\u0432\u0438 \u0440\u0435\u0439\u043d \u0437\u0430 \u0433\u0440\u0430\u043d\u044c\u044e \u0434\u0432\u0435 \u0434\u0443\u0448\u0438",
            "\u0445\u0435\u0432\u0438 \u0440\u0435\u0439\u043d \u0438 beyond two souls",
            "\u0445\u0435\u0432\u0438 \u0440\u0435\u0439\u043d beyond two souls",
            "heavy rain \u0438 \u0437\u0430 \u0433\u0440\u0430\u043d\u044c\u044e \u0434\u0432\u0435 \u0434\u0443\u0448\u0438",
            "heavy rain \u0437\u0430 \u0433\u0440\u0430\u043d\u044c\u044e \u0434\u0432\u0435 \u0434\u0443\u0448\u0438",
            "heavy rain \u0438 beyond two souls",
        ],
    }
)
EXTRA_ITEMS.update(
    {
        "heavy_rain_and_beyond_two_souls": {
            "id": "heavy_rain_and_beyond_two_souls",
            "canonical_name": "Heavy Rain And Beyond Two Souls",
            "platform": "ps4",
            "item_type": "game",
            "category": "manual",
            "confidence": "high",
            "sort_weight": 980,
        },
    }
)


def normalize_catalog_text(value: Any) -> str:
    text = str(value or "").lower().replace("\u0451", "\u0435")
    text = text.replace("&", " and ").replace("+", " plus ")
    text = re.sub(r"([0-9])([a-z\u0430-\u044f\u0451])", r"\1 \2", text)
    text = re.sub(r"([a-z\u0430-\u044f\u0451])([0-9])", r"\1 \2", text)
    return " ".join(_TOKEN_RE.findall(text))


def catalog_tokens(value: str) -> set[str]:
    return {token for token in normalize_catalog_text(value).split() if token and token not in _NOISE_TOKENS}


@lru_cache(maxsize=1)
def load_catalog() -> dict[str, Any]:
    loaded_from_crm = load_catalog_from_crm()
    if loaded_from_crm is not None:
        return loaded_from_crm
    if not CATALOG_PATH.exists():
        return {"items": [], "alias_index": []}
    loaded = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        return {"items": [], "alias_index": []}
    return loaded


def load_catalog_from_crm() -> dict[str, Any] | None:
    try:
        with urlopen(CRM_CATALOG_URL, timeout=5.0) as response:
            if response.status < 200 or response.status >= 300:
                return None
            loaded = json.loads(response.read().decode("utf-8", errors="replace"))
    except (OSError, URLError, TimeoutError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, dict):
        return None
    if not isinstance(loaded.get("items"), list):
        return None
    if not isinstance(loaded.get("alias_index"), list):
        loaded["alias_index"] = []
    return loaded


@lru_cache(maxsize=1)
def catalog_items_by_id() -> dict[str, dict[str, Any]]:
    items = load_catalog().get("items", [])
    if not isinstance(items, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        if isinstance(item, dict) and item.get("id"):
            result[str(item["id"])] = item
    result.update(EXTRA_ITEMS)
    return result


@lru_cache(maxsize=1)
def catalog_search_index() -> list[dict[str, Any]]:
    loaded = load_catalog()
    alias_index = loaded.get("alias_index", [])
    entries: list[dict[str, Any]] = []
    if isinstance(alias_index, list):
        for raw in alias_index:
            if not isinstance(raw, dict):
                continue
            normalized = normalize_catalog_text(raw.get("normalized") or raw.get("term"))
            if not normalized:
                continue
            entries.append(
                {
                    "item_id": str(raw.get("item_id") or raw.get("game_id") or ""),
                    "canonical_name": str(raw.get("canonical_name") or ""),
                    "item_type": str(raw.get("item_type") or "unknown"),
                    "term": str(raw.get("term") or raw.get("canonical_name") or ""),
                    "normalized": normalized,
                    "tokens": catalog_tokens(normalized),
                    "weight": int(raw.get("weight") or 0),
                }
            )
    items = list(loaded.get("items", []) or []) + list(EXTRA_ITEMS.values())
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            item_id = str(item["id"])
            raw_terms = [item.get("canonical_name"), *list(item.get("aliases") or [])]
            for term in raw_terms:
                normalized = normalize_catalog_text(term)
                if not normalized:
                    continue
                entries.append(
                    {
                        "item_id": item_id,
                        "canonical_name": str(item.get("canonical_name") or ""),
                        "item_type": str(item.get("item_type") or "unknown"),
                        "term": str(term or ""),
                        "normalized": normalized,
                        "tokens": catalog_tokens(normalized),
                        "weight": int(item.get("sort_weight") or 0),
                    }
                )
            for term in EXTRA_ALIASES.get(item_id, []):
                normalized = normalize_catalog_text(term)
                if not normalized:
                    continue
                entries.append(
                    {
                        "item_id": item_id,
                        "canonical_name": str(item.get("canonical_name") or ""),
                        "item_type": str(item.get("item_type") or "unknown"),
                        "term": term,
                        "normalized": normalized,
                        "tokens": catalog_tokens(normalized),
                        "weight": int(item.get("sort_weight") or 0) + 200,
                    }
                )
    return entries


def catalog_candidates_for_record(record: object, *, limit: int = 40) -> list[dict[str, object]]:
    text = " ".join(
        str(part or "")
        for part in [
            getattr(record, "title", None),
            getattr(record, "description", None),
            " ".join(getattr(record, "raw_card_texts", []) or []),
            " ".join(getattr(record, "raw_detail_texts", []) or []),
        ]
    )
    return catalog_candidates_for_text(text, limit=limit)


def catalog_candidates_for_text(text: str, *, limit: int = 40) -> list[dict[str, object]]:
    normalized_text = normalize_catalog_text(text)
    query_tokens = catalog_tokens(normalized_text)
    if not normalized_text or not query_tokens:
        return []

    scored: dict[str, dict[str, Any]] = {}
    for entry in catalog_search_index():
        entry_tokens = entry["tokens"]
        if not entry_tokens:
            continue
        score = score_catalog_entry(normalized_text, query_tokens, entry)
        if score <= 0:
            continue
        item_id = str(entry["item_id"])
        current = scored.get(item_id)
        if current is None or score > current["score"]:
            item = catalog_items_by_id().get(item_id, {})
            scored[item_id] = {
                "catalog_item_id": item_id,
                "canonical_name": entry["canonical_name"] or item.get("canonical_name"),
                "item_type": entry["item_type"] or item.get("item_type"),
                "platform": item.get("platform"),
                "category": item.get("category"),
                "confidence": item.get("confidence"),
                "matched_alias": entry["term"],
                "score": score,
            }
    candidates = sorted(scored.values(), key=lambda item: (-int(item["score"]), str(item.get("canonical_name") or "")))
    return candidates[:limit]


def score_catalog_entry(normalized_text: str, query_tokens: set[str], entry: dict[str, Any]) -> int:
    alias = str(entry["normalized"])
    alias_tokens = set(entry["tokens"])
    if not alias_tokens:
        return 0
    weight = int(entry.get("weight") or 0)
    if f" {alias} " in f" {normalized_text} ":
        return 10000 + weight + len(alias_tokens) * 100
    overlap = len(alias_tokens & query_tokens)
    if overlap == 0:
        return 0
    coverage = overlap / len(alias_tokens)
    if coverage >= 1.0 and len(alias_tokens) >= 2:
        return 5000 + weight + len(alias_tokens) * 50
    if coverage >= 0.75 and len(alias_tokens) >= 3:
        return 2500 + weight + overlap * 40
    return 0
