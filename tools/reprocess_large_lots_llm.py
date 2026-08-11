from __future__ import annotations

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json


ROOT = Path(__file__).resolve().parents[1]
DB_DSN = os.environ.get("CRM_POSTGRES_DSN") or "postgresql://postgres:postgres@127.0.0.1:5432/crm_inventory"
AUDIT_PATH = ROOT / "reports" / "market_price_and_large_lot_audit_20260810.json"
REPORT_PATH = ROOT / "reports" / "large_lot_llm_reprocess_20260810.json"
MAX_WORKERS = int(os.environ.get("LARGE_LOT_LLM_WORKERS") or "6")

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.crm_market import lot_cost_observations_from_items, market_price_observations_from_items
from app.llm_analyzer import extract_listing_items_if_configured, interpret_listing_price_for_record, llm_model
from app.monitor import NewListingRecord, seller_city_from_address
from app.pipeline import infer_item_kind, load_automation_settings


def json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value).replace("\xa0", " ")).strip()
    return text or None


def record_from_row(row: dict[str, Any]) -> NewListingRecord:
    raw = row.get("raw_json") if isinstance(row.get("raw_json"), dict) else {}
    raw_card_texts = raw.get("raw_card_texts") if isinstance(raw.get("raw_card_texts"), list) else []
    raw_detail_texts = raw.get("raw_detail_texts") if isinstance(raw.get("raw_detail_texts"), list) else []
    description = clean_text(row.get("description") or raw.get("original_description"))
    title = clean_text(row.get("title"))
    address = clean_text(row.get("address"))
    record = NewListingRecord(
        saved_at=clean_text(raw.get("bot_saved_at") or raw.get("saved_at") or row.get("scraped_at")) or datetime.now().isoformat(timespec="seconds"),
        fingerprint=clean_text(raw.get("fingerprint")) or f"market-listing-{row['id']}",
        title=title,
        price=int(row["price"]) if row.get("price") is not None else None,
        address=address,
        description=description,
        url=clean_text(row.get("url")),
        raw_card_texts=[str(item) for item in raw_card_texts],
        raw_detail_texts=[str(item) for item in raw_detail_texts],
        delivery_text=clean_text(raw.get("delivery_text")),
        delivery_price_rub=int(raw["delivery_price_rub"]) if raw.get("delivery_price_rub") is not None else None,
        delivery_status=clean_text(raw.get("delivery_status")),
        seller_city=clean_text(raw.get("seller_city")) or seller_city_from_address(address),
        item_kind=clean_text(row.get("type") or raw.get("item_kind")) or infer_item_kind(title, description),
    )
    setattr(record, "external_id", clean_text(row.get("external_id")))
    if isinstance(raw.get("monitor_context"), dict):
        setattr(record, "monitor_context", raw["monitor_context"])
    return record


def item_count(items: Any) -> int:
    return len(items) if isinstance(items, list) else 0


def process_one(row: dict[str, Any], settings: dict[str, object]) -> dict[str, Any]:
    raw_json = row.get("raw_json") if isinstance(row.get("raw_json"), dict) else {}
    raw_json = dict(raw_json)
    old_items = raw_json.get("llm_extracted_items") if isinstance(raw_json.get("llm_extracted_items"), list) else []
    record = record_from_row(row)
    new_items = extract_listing_items_if_configured(record, settings, original_description=record.description)
    if new_items is None:
        new_items = old_items
    raw_json.setdefault("llm_large_lot_backups", [])
    if isinstance(raw_json["llm_large_lot_backups"], list):
        raw_json["llm_large_lot_backups"].append(
            {
                "saved_at": datetime.now().isoformat(timespec="seconds"),
                "items": old_items,
                "llm_listing_price_interpretation": raw_json.get("llm_listing_price_interpretation"),
                "llm_market_price_observations": raw_json.get("llm_market_price_observations"),
                "llm_lot_cost_observations": raw_json.get("llm_lot_cost_observations"),
            }
        )
    setattr(record, "llm_extracted_items", new_items)
    raw_json["llm_extracted_items"] = new_items
    raw_json["llm_listing_price_interpretation"] = interpret_listing_price_for_record(record)
    raw_json["llm_market_price_observations"] = market_price_observations_from_items(new_items)
    raw_json["llm_lot_cost_observations"] = lot_cost_observations_from_items(new_items)
    raw_json["llm_large_lot_reprocessed_at"] = datetime.now().isoformat(timespec="seconds")
    raw_json["llm_large_lot_reprocess_run_id"] = "large_lot_llm_20260810"

    with psycopg.connect(DB_DSN) as conn:
        conn.execute(
            "update market_listings set raw_json = %s where id = %s",
            (Json(raw_json, dumps=lambda value: json.dumps(value, ensure_ascii=False, default=json_default)), row["id"]),
        )
        conn.commit()

    return {
        "id": row["id"],
        "external_id": row.get("external_id"),
        "title": row.get("title"),
        "old_items_count": item_count(old_items),
        "new_items_count": item_count(new_items),
        "old_game_qty": sum(int(item.get("quantity") or 1) for item in old_items if isinstance(item, dict) and item.get("item_type") == "game"),
        "new_game_qty": sum(int(item.get("quantity") or 1) for item in new_items if isinstance(item, dict) and item.get("item_type") == "game"),
        "new_items_sample": [
            {
                "name": item.get("name"),
                "item_type": item.get("item_type"),
                "quantity": item.get("quantity"),
                "price_rub": item.get("price_rub"),
                "status": item.get("extraction_status"),
                "missing": item.get("missing_data_reason"),
            }
            for item in new_items[:20]
            if isinstance(item, dict)
        ],
    }


def main() -> None:
    if not AUDIT_PATH.exists():
        raise RuntimeError(f"Audit report not found: {AUDIT_PATH}")
    audit = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
    external_ids = [str(row["external_id"]) for row in audit.get("large_lot_suspects", []) if row.get("external_id")]
    if not external_ids:
        print("[LARGE-LOT] no suspects")
        return
    settings = load_automation_settings()
    print(f"[LARGE-LOT] model={llm_model(settings)} suspects={len(external_ids)} workers={MAX_WORKERS}")
    with psycopg.connect(DB_DSN, row_factory=dict_row) as conn:
        rows = list(
            conn.execute(
                """
                select id, external_id, title, description, url, price, currency, address,
                       platform, format, type, posted_at, scraped_at, raw_json::jsonb as raw_json
                  from market_listings
                 where external_id = any(%s)
                 order by id
                """,
                (external_ids,),
            )
        )
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_one, row, settings): row for row in rows}
        for index, future in enumerate(as_completed(futures), start=1):
            row = futures[future]
            try:
                result = future.result()
            except Exception as error:  # noqa: BLE001
                result = {
                    "id": row["id"],
                    "external_id": row.get("external_id"),
                    "title": row.get("title"),
                    "error": str(error),
                }
            results.append(result)
            print(f"[LARGE-LOT] {index}/{len(rows)} {result.get('external_id')} {result.get('old_items_count')} -> {result.get('new_items_count')} {result.get('error') or ''}")
    payload = {
        "model": llm_model(settings),
        "workers": MAX_WORKERS,
        "processed": len(results),
        "errors": sum(1 for row in results if row.get("error")),
        "improved_item_count": sum(1 for row in results if (row.get("new_items_count") or 0) > (row.get("old_items_count") or 0)),
        "results": sorted(results, key=lambda row: str(row.get("external_id") or "")),
    }
    REPORT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")
    print(json.dumps({k: payload[k] for k in ("processed", "errors", "improved_item_count")}, ensure_ascii=False, indent=2))
    print(f"[REPORT] {REPORT_PATH}")


if __name__ == "__main__":
    main()
