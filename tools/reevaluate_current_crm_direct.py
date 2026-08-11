from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json


ROOT = Path(__file__).resolve().parents[1]
CRM_ROOT = Path("C:/crm_inventory")
DB_DSN = os.environ.get("CRM_POSTGRES_DSN") or "postgresql://postgres:postgres@127.0.0.1:5432/crm_inventory"
REPORT_PATH = ROOT / "reports" / "crm_direct_reevaluation_20260810.json"


def json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def load_crm_evaluator():
    sys.modules.pop("app", None)
    sys.path = [str(CRM_ROOT)] + [p for p in sys.path if p not in {str(ROOT), str(CRM_ROOT)}]
    from app.avito_evaluator import evaluate_avito_listing_payload
    from app.config import get_settings
    from app.db import get_engine, make_session_factory

    engine = get_engine(get_settings())
    session_factory = make_session_factory(engine)
    return evaluate_avito_listing_payload, session_factory


def listing_payload(row: dict[str, Any], raw_json: dict[str, Any]) -> dict[str, Any]:
    return {
        "external_id": row.get("external_id"),
        "title": row.get("title"),
        "description": row.get("description"),
        "url": row.get("url"),
        "price": float(row["price"]) if row.get("price") is not None else None,
        "currency": row.get("currency") or "RUB",
        "address": row.get("address"),
        "platform": row.get("platform"),
        "format": row.get("format"),
        "type": row.get("type"),
        "posted_at": row.get("posted_at").isoformat() if hasattr(row.get("posted_at"), "isoformat") else row.get("posted_at"),
        "scraped_at": row.get("scraped_at").isoformat() if hasattr(row.get("scraped_at"), "isoformat") else row.get("scraped_at"),
        "delivery_price_rub": raw_json.get("delivery_price_rub"),
        "delivery_price": raw_json.get("delivery_price_rub"),
        "delivery_status": raw_json.get("delivery_status"),
        "raw_json": raw_json,
    }


def profit(evaluation: dict[str, Any] | None) -> float | None:
    if not isinstance(evaluation, dict):
        return None
    total = evaluation.get("total")
    if isinstance(total, dict) and isinstance(total.get("expected_profit"), (int, float)):
        return float(total["expected_profit"])
    return None


def main() -> None:
    evaluate_avito_listing_payload, session_factory = load_crm_evaluator()
    started_at = datetime.now().isoformat(timespec="seconds")
    report_rows: list[dict[str, Any]] = []

    with psycopg.connect(DB_DSN, row_factory=dict_row) as conn:
        rows = list(
            conn.execute(
                """
                select id, external_id, title, description, url, price, currency, address,
                       platform, format, type, posted_at, scraped_at, raw_json::jsonb as raw_json
                  from market_listings
                 order by id
                """
            )
        )

    with session_factory() as session:
        for index, row in enumerate(rows, start=1):
            raw_json = row.get("raw_json") if isinstance(row.get("raw_json"), dict) else {}
            raw_json = dict(raw_json)
            extracted_items = raw_json.get("llm_extracted_items")
            if not isinstance(extracted_items, list):
                extracted_items = []
            old_eval = raw_json.get("crm_evaluation_result_current")
            evaluation = evaluate_avito_listing_payload(
                session,
                listing=listing_payload(row, raw_json),
                extracted_items=extracted_items,
            )
            raw_json.setdefault("crm_evaluation_backups", {})
            if isinstance(raw_json["crm_evaluation_backups"], dict):
                raw_json["crm_evaluation_backups"]["before_20260810_direct_reevaluation"] = old_eval
            raw_json["crm_evaluation_result_current"] = evaluation
            raw_json["crm_evaluation_reprocessed_at"] = datetime.now().isoformat(timespec="seconds")
            raw_json["crm_evaluation_reprocess_run_id"] = "direct_reevaluation_20260810"

            with psycopg.connect(DB_DSN) as update_conn:
                update_conn.execute(
                    "update market_listings set raw_json = %s where id = %s",
                    (Json(raw_json, dumps=lambda value: json.dumps(value, ensure_ascii=False, default=json_default)), row["id"]),
                )
                update_conn.commit()

            report_rows.append(
                {
                    "id": row["id"],
                    "external_id": row.get("external_id"),
                    "title": row.get("title"),
                    "items_count": len(extracted_items),
                    "old_decision": old_eval.get("decision") if isinstance(old_eval, dict) else None,
                    "new_decision": evaluation.get("decision"),
                    "old_profit": profit(old_eval),
                    "new_profit": profit(evaluation),
                    "risks": evaluation.get("risks"),
                }
            )
            if index % 100 == 0:
                print(f"[REEVAL] {index}/{len(rows)}")

    comparable = [row for row in report_rows if row["old_profit"] is not None and row["new_profit"] is not None]
    summary = {
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "processed": len(report_rows),
        "comparable": len(comparable),
        "decision_changed": sum(1 for row in comparable if row["old_decision"] != row["new_decision"]),
        "became_skip": sum(1 for row in comparable if row["old_decision"] != "skip" and row["new_decision"] == "skip"),
        "became_good_or_check": sum(1 for row in comparable if row["old_decision"] == "skip" and row["new_decision"] in {"good", "check"}),
        "profit_delta_sum": round(sum(row["new_profit"] - row["old_profit"] for row in comparable), 2),
        "profit_down_count": sum(1 for row in comparable if row["new_profit"] < row["old_profit"]),
        "profit_up_count": sum(1 for row in comparable if row["new_profit"] > row["old_profit"]),
    }
    payload = {
        "summary": summary,
        "top_profit_down": sorted(comparable, key=lambda row: row["new_profit"] - row["old_profit"])[:50],
        "top_profit_up": sorted(comparable, key=lambda row: row["new_profit"] - row["old_profit"], reverse=True)[:50],
        "decision_changed": [row for row in comparable if row["old_decision"] != row["new_decision"]],
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"[REPORT] {REPORT_PATH}")


if __name__ == "__main__":
    main()
