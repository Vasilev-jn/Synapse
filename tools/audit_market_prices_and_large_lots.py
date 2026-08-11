from __future__ import annotations

import json
import os
import re
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row


ROOT = Path(__file__).resolve().parents[1]
DB_DSN = os.environ.get("CRM_POSTGRES_DSN") or "postgresql://postgres:postgres@127.0.0.1:5432/crm_inventory"
REPORT_PATH = ROOT / "reports" / "market_price_and_large_lot_audit_20260810.json"


JUNK_MARKERS = (
    "скупка",
    "выкуп",
    "куплю",
    "оценю",
    "ремонт",
    "услуга",
    "услуги",
    "создание профиля",
    "пополнение",
    "активация",
    "подписка",
    "аренда",
    "прокат",
    "аккаунт",
    "цифров",
    "digital",
    "ключ",
)

BIG_LOT_MARKERS = (
    "много игр",
    "куча игр",
    "комплект игр",
    "с играми",
    "диски",
    "игры:",
    "игры ps4",
    "игры для ps4",
    "штук",
    "шт.",
)

WATCH_NAMES = (
    "PlayStation 4 Fat 500GB",
    "PlayStation 4 Slim 500GB",
    "PlayStation 4 Slim 1TB",
    "PlayStation 4 Pro 1TB",
    "PlayStation 5 Digital",
    "PlayStation 5 Slim",
    "DualShock 4",
    "DualSense",
    "Зарядная станция DualShock 4",
)


def json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").lower().replace("ё", "е")).strip()


def price_line_count(text: str) -> int:
    count = 0
    for line in re.split(r"[\n\r;]+", text):
        cleaned = norm(line)
        if not cleaned:
            continue
        if re.search(r"\b\d{2,6}\s*(?:₽|р|руб|rub)\b", cleaned) and re.search(r"[a-zа-я]{3,}", cleaned):
            count += 1
    return count


def explicit_quantity(text: str) -> int | None:
    candidates: list[int] = []
    for match in re.finditer(r"(\d{1,3})\s*(?:игр|игры|диск|дисков|штук|шт\b)", text, re.IGNORECASE):
        value = int(match.group(1))
        if 2 <= value <= 100:
            candidates.append(value)
    return max(candidates) if candidates else None


def listing_text(row: dict[str, Any]) -> str:
    raw = row.get("raw_json") if isinstance(row.get("raw_json"), dict) else {}
    parts: list[str] = [str(row.get("title") or ""), str(row.get("description") or "")]
    for key in ("raw_card_texts", "raw_detail_texts"):
        value = raw.get(key)
        if isinstance(value, list):
            parts.extend(str(item) for item in value)
    return "\n".join(parts)


def item_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    types = Counter(str(item.get("item_type") or "unknown") for item in items if isinstance(item, dict))
    physical = sum(1 for item in items if isinstance(item, dict) and item.get("is_physical") is not False)
    games = sum(int(item.get("quantity") or 1) for item in items if isinstance(item, dict) and item.get("item_type") == "game")
    return {"count": len(items), "types": dict(types), "physical_count": physical, "game_quantity": games}


def median_payload(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "min": None, "median": None, "avg": None, "max": None}
    ordered = sorted(values)
    return {
        "n": len(values),
        "min": round(ordered[0]),
        "median": round(statistics.median(ordered)),
        "avg": round(sum(ordered) / len(ordered)),
        "max": round(ordered[-1]),
    }


def main() -> None:
    with psycopg.connect(DB_DSN, row_factory=dict_row) as conn:
        listings = list(
            conn.execute(
                """
                select id, external_id, title, description, url, price, raw_json::jsonb as raw_json
                  from market_listings
                 order by id
                """
            )
        )
        observations = list(
            conn.execute(
                """
                select po.id, po.item_title, po.price, po.observation_type,
                       po.source, po.usable_for_auto_price, po.raw_json::jsonb as raw_json,
                       ce.name as canonical_name
                  from price_observations po
                  left join catalog_entries ce on ce.id = po.catalog_entry_id
                 order by po.id
                """
            )
        )

    large_lot_suspects: list[dict[str, Any]] = []
    subscription_bundles: list[dict[str, Any]] = []
    junk_eval_still_positive: list[dict[str, Any]] = []
    for row in listings:
        raw = row.get("raw_json") if isinstance(row.get("raw_json"), dict) else {}
        items = raw.get("llm_extracted_items") if isinstance(raw.get("llm_extracted_items"), list) else []
        text = listing_text(row)
        normalized_text = norm(text)
        qty = explicit_quantity(normalized_text)
        lines = price_line_count(text)
        summary = item_summary(items)
        has_big_marker = any(marker in normalized_text for marker in BIG_LOT_MARKERS)
        expected_min = max([value for value in (qty, lines) if value is not None] or [0])
        if has_big_marker and (expected_min >= 5 or lines >= 5) and summary["count"] < max(3, min(expected_min, 12)):
            large_lot_suspects.append(
                {
                    "external_id": row.get("external_id"),
                    "title": row.get("title"),
                    "url": row.get("url"),
                    "price": float(row["price"]) if row.get("price") is not None else None,
                    "explicit_quantity": qty,
                    "price_lines": lines,
                    "items": summary,
                    "sample_text": re.sub(r"\s+", " ", text)[:700],
                }
            )
        if "account_subscription_bundle_low_confidence_bonus" in raw.get("crm_evaluation_result_current", {}).get("risks", []):
            subscription_bundles.append(
                {
                    "external_id": row.get("external_id"),
                    "title": row.get("title"),
                    "url": row.get("url"),
                    "price": float(row["price"]) if row.get("price") is not None else None,
                    "items": summary,
                    "profit": raw.get("crm_evaluation_result_current", {}).get("total", {}).get("expected_profit"),
                    "decision": raw.get("crm_evaluation_result_current", {}).get("decision"),
                }
            )
        eval_current = raw.get("crm_evaluation_result_current") if isinstance(raw.get("crm_evaluation_result_current"), dict) else {}
        profit = eval_current.get("total", {}).get("expected_profit") if isinstance(eval_current.get("total"), dict) else None
        if any(marker in normalized_text for marker in JUNK_MARKERS) and isinstance(profit, (int, float)) and profit > 0 and eval_current.get("decision") != "skip":
            junk_eval_still_positive.append(
                {
                    "external_id": row.get("external_id"),
                    "title": row.get("title"),
                    "url": row.get("url"),
                    "profit": profit,
                    "decision": eval_current.get("decision"),
                    "risks": eval_current.get("risks"),
                    "items": summary,
                }
            )

    obs_by_name: dict[str, list[float]] = defaultdict(list)
    usable_by_name: dict[str, list[float]] = defaultdict(list)
    junk_observations: list[dict[str, Any]] = []
    type_counts = Counter()
    usable_type_counts = Counter()
    for obs in observations:
        raw = obs.get("raw_json") if isinstance(obs.get("raw_json"), dict) else {}
        name = str(obs.get("canonical_name") or raw.get("canonical_name") or raw.get("matched_name") or obs.get("item_title") or "").strip()
        item_type = str(raw.get("item_type") or "unknown")
        type_counts[item_type] += 1
        if obs.get("usable_for_auto_price"):
            usable_type_counts[item_type] += 1
        price = obs.get("price")
        if price is not None and name:
            price_float = float(price)
            obs_by_name[name].append(price_float)
            if obs.get("usable_for_auto_price"):
                usable_by_name[name].append(price_float)
        obs_text = norm(" ".join(str(raw.get(k) or "") for k in ("name", "canonical_name", "matched_name", "item_type", "source_text", "notes")) + " " + str(obs.get("item_title") or ""))
        if obs.get("usable_for_auto_price") and (item_type in {"digital_account", "subscription", "service", "account", "rent", "rental"} or any(marker in obs_text for marker in JUNK_MARKERS)):
            junk_observations.append(
                {
                    "id": obs.get("id"),
                    "item_title": obs.get("item_title"),
                    "canonical_name": obs.get("canonical_name"),
                    "item_type": item_type,
                    "price": float(price) if price is not None else None,
                    "source": obs.get("source"),
                    "observation_type": obs.get("observation_type"),
                    "raw_json": raw,
                }
            )

    watched = {}
    for watch in WATCH_NAMES:
        matching_names = [name for name in obs_by_name if norm(watch) in norm(name) or norm(name) in norm(watch)]
        values = [price for name in matching_names for price in obs_by_name[name]]
        usable_values = [price for name in matching_names for price in usable_by_name[name]]
        watched[watch] = {
            "matched_names": sorted(matching_names),
            "all": median_payload(values),
            "usable": median_payload(usable_values),
        }

    top_usable = sorted(
        (
            {"name": name, **median_payload(values)}
            for name, values in usable_by_name.items()
        ),
        key=lambda row: row["n"],
        reverse=True,
    )[:80]

    payload = {
        "summary": {
            "listings": len(listings),
            "price_observations": len(observations),
            "usable_price_observations": sum(1 for obs in observations if obs.get("usable_for_auto_price")),
            "large_lot_suspects": len(large_lot_suspects),
            "subscription_bundles": len(subscription_bundles),
            "junk_observations_usable": len(junk_observations),
            "junk_positive_evaluations": len(junk_eval_still_positive),
            "observation_type_counts": dict(type_counts),
            "usable_observation_type_counts": dict(usable_type_counts),
        },
        "watched_medians": watched,
        "top_usable_medians": top_usable,
        "large_lot_suspects": large_lot_suspects[:100],
        "subscription_bundles": subscription_bundles[:100],
        "junk_observations_usable": junk_observations[:100],
        "junk_positive_evaluations": junk_eval_still_positive[:100],
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    print(f"[REPORT] {REPORT_PATH}")


if __name__ == "__main__":
    main()
