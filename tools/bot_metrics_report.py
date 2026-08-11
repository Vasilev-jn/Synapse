from __future__ import annotations

import json
import re
import statistics
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
LOCAL_CRM_ROOT = ROOT / "crm"
CRM_ROOT = LOCAL_CRM_ROOT if LOCAL_CRM_ROOT.exists() else Path(r"C:\crm_inventory")
REPORT_JSON = ROOT / "reports" / "bot_metrics_latest.json"
REPORT_MD = ROOT / "reports" / "bot_metrics_latest.md"
MANUAL_AUDIT_PATH = ROOT / "reports" / "llm_manual_audit.jsonl"


def add_crm_to_path() -> None:
    sys.path.insert(0, str(CRM_ROOT))


def normalize_name(value: Any) -> str:
    text = str(value or "").lower().replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def item_name(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("canonical_name") or item.get("name") or item.get("title") or "")
    return str(item or "")


def counter_from_items(items: Any) -> Counter[str]:
    counter: Counter[str] = Counter()
    if not isinstance(items, list):
        return counter
    for item in items:
        name = normalize_name(item_name(item))
        if name:
            counter[name] += 1
    return counter


def percent(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value * 100, 2)


def safe_ratio(numerator: float, denominator: float) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator


def metric_summary(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "avg": None, "median": None, "min": None, "max": None}
    return {
        "n": len(values),
        "avg": round(sum(values) / len(values), 4),
        "median": round(statistics.median(values), 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
    }


def load_manual_llm_metrics() -> dict[str, Any]:
    if not MANUAL_AUDIT_PATH.exists():
        return {
            "sample_size": 0,
            "expected_items": 0,
            "predicted_items": 0,
            "missed_items": 0,
            "hallucinated_items": 0,
            "llm_miss_rate_percent": None,
            "llm_hallucination_rate_percent": None,
            "note": f"Для точного LLM Miss/Hallucination заполни {MANUAL_AUDIT_PATH}",
            "manual_audit_format": {
                "listing_id": 123,
                "url": "https://www.avito.ru/...",
                "expected_items": [{"name": "GTA V"}, {"name": "DualShock 4"}],
                "extracted_items": [{"name": "GTA V"}],
            },
        }

    sample_size = 0
    expected_total = 0
    predicted_total = 0
    missed_total = 0
    hallucinated_total = 0
    for line in MANUAL_AUDIT_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        expected = counter_from_items(row.get("expected_items"))
        predicted = counter_from_items(row.get("extracted_items") or row.get("predicted_items"))
        sample_size += 1
        expected_total += sum(expected.values())
        predicted_total += sum(predicted.values())
        for name, count in expected.items():
            missed_total += max(0, count - predicted.get(name, 0))
        for name, count in predicted.items():
            hallucinated_total += max(0, count - expected.get(name, 0))

    return {
        "sample_size": sample_size,
        "expected_items": expected_total,
        "predicted_items": predicted_total,
        "missed_items": missed_total,
        "hallucinated_items": hallucinated_total,
        "llm_miss_rate_percent": percent(safe_ratio(missed_total, expected_total) or 0) if expected_total else None,
        "llm_hallucination_rate_percent": percent(safe_ratio(hallucinated_total, predicted_total) or 0)
        if predicted_total
        else None,
        "source": str(MANUAL_AUDIT_PATH),
    }


def load_crm_metrics() -> dict[str, Any]:
    add_crm_to_path()
    from sqlalchemy import select
    from sqlalchemy.orm import sessionmaker

    from app.db import get_engine
    from app.models import Item, MarketListing, PriceObservation

    engine = get_engine()
    Session = sessionmaker(bind=engine, future=True)
    with Session() as session:
        listings = session.execute(select(MarketListing)).scalars().all()
        observations = session.execute(select(PriceObservation)).scalars().all()
        own_items = session.execute(select(Item)).scalars().all()

    llm_item_total = 0
    crm_unmatched_proxy = 0
    no_concrete_name = 0
    junk_like_items = 0
    listing_with_llm = 0
    for listing in listings:
        raw = listing.raw_json if isinstance(listing.raw_json, dict) else {}
        items = raw.get("llm_extracted_items")
        if not isinstance(items, list):
            continue
        listing_with_llm += 1
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("canonical_name") or "")
            item_type = str(item.get("item_type") or "")
            combined = normalize_name(" ".join(str(item.get(key) or "") for key in ("name", "item_type", "notes", "buyer_thoughts")))
            if not name or "unknown" in name.lower():
                no_concrete_name += 1
            if not item.get("canonical_name") or not item.get("catalog_item_id"):
                crm_unmatched_proxy += 1
            if any(marker in combined for marker in ("скупка", "аренда", "прокат", "ремонт", "услуга", "цифров", "аккаунт")):
                junk_like_items += 1
            if item_type:
                llm_item_total += int(item.get("quantity") or 1)
            else:
                llm_item_total += 1

    variance_values: list[float] = []
    realization_values: list[float] = []
    for item in own_items:
        sale_price = getattr(item, "sale_price", None)
        expected_sale_price = getattr(item, "expected_sale_price", None)
        calculated_cost = getattr(item, "calculated_cost", None)
        if sale_price and expected_sale_price:
            variance_values.append((float(sale_price) / float(expected_sale_price)) - 1)
        if sale_price and expected_sale_price and calculated_cost is not None:
            expected_profit = float(expected_sale_price) - float(calculated_cost)
            actual_profit = float(sale_price) - float(calculated_cost)
            if expected_profit:
                realization_values.append(actual_profit / expected_profit)

    usable_observations = [obs for obs in observations if getattr(obs, "usable_for_auto_price", False)]
    return {
        "market_listings_total": len(listings),
        "market_listings_with_llm_items": listing_with_llm,
        "llm_items_total": llm_item_total,
        "crm_quarantine_proxy_items": crm_unmatched_proxy,
        "crm_quarantine_proxy_rate_percent": percent(safe_ratio(crm_unmatched_proxy, llm_item_total) or 0)
        if llm_item_total
        else None,
        "no_concrete_name_items": no_concrete_name,
        "junk_like_llm_items": junk_like_items,
        "price_observations_total": len(observations),
        "price_observations_usable_for_auto_price": len(usable_observations),
        "price_observations_usable_rate_percent": percent(safe_ratio(len(usable_observations), len(observations)) or 0)
        if observations
        else None,
        "price_variance": metric_summary(variance_values),
        "profit_realization_rate": metric_summary(realization_values),
        "notes": {
            "crm_quarantine_proxy": "Точный CRM quarantine появится, когда будем сохранять результат evaluate в БД. Сейчас это proxy: LLM item без canonical_name/catalog_item_id.",
            "price_variance": "(фактическая цена продажи / ожидаемая цена продажи) - 1 по личным items с sale_price и expected_sale_price.",
            "profit_realization_rate": "(sale_price - calculated_cost) / (expected_sale_price - calculated_cost) по личным items.",
        },
    }


def render_markdown(report: dict[str, Any]) -> str:
    crm = report["crm"]
    llm = report["llm_manual"]
    lines = [
        "# Bot metrics latest",
        "",
        f"Generated at: `{report['generated_at']}`",
        "",
        "## LLM quality",
        "",
        f"- Manual sample size: {llm['sample_size']}",
        f"- LLM Miss Rate: {llm['llm_miss_rate_percent']}%",
        f"- LLM Hallucination Rate: {llm['llm_hallucination_rate_percent']}%",
        "",
        "## CRM / market quality",
        "",
        f"- Market listings: {crm['market_listings_total']}",
        f"- Listings with LLM items: {crm['market_listings_with_llm_items']}",
        f"- LLM items total: {crm['llm_items_total']}",
        f"- CRM quarantine proxy: {crm['crm_quarantine_proxy_items']} ({crm['crm_quarantine_proxy_rate_percent']}%)",
        f"- Price observations: {crm['price_observations_total']}",
        f"- Usable auto-price observations: {crm['price_observations_usable_for_auto_price']} ({crm['price_observations_usable_rate_percent']}%)",
        "",
        "## Sales reality check",
        "",
        f"- Price variance median: {crm['price_variance']['median']}",
        f"- Profit realization median: {crm['profit_realization_rate']['median']}",
        "",
        "Note: LLM Miss/Hallucination are only exact after manual labels in `reports/llm_manual_audit.jsonl`.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    report = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "crm": load_crm_metrics(),
        "llm_manual": load_manual_llm_metrics(),
    }
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    REPORT_MD.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
