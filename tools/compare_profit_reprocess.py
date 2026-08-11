from __future__ import annotations

import json
import os
from pathlib import Path
import statistics
from typing import Any

import psycopg
from psycopg.rows import dict_row


DB_DSN = os.environ.get("CRM_POSTGRES_DSN") or "postgresql://postgres:postgres@127.0.0.1:5432/crm_inventory"
REPORT_PATH = Path("reports") / "profit_reprocess_comparison_20260809.json"


def expected_profit(payload: Any) -> float | None:
    if not isinstance(payload, dict):
        return None
    total = payload.get("total")
    if isinstance(total, dict) and isinstance(total.get("expected_profit"), (int, float)):
        return float(total["expected_profit"])
    return None


def decision(payload: Any) -> str | None:
    if isinstance(payload, dict) and payload.get("decision") is not None:
        return str(payload.get("decision"))
    return None


def item_count(raw: dict[str, Any], key_path: str) -> int | None:
    value: Any = raw
    for key in key_path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    if isinstance(value, list):
        return len(value)
    return None


def load_rows() -> list[dict[str, Any]]:
    with psycopg.connect(DB_DSN, row_factory=dict_row) as conn:
        return list(
            conn.execute(
                """
                select id, external_id, title, price, url, raw_json::jsonb as raw_json
                from market_listings
                order by id
                """
            )
        )


def run() -> dict[str, Any]:
    rows = load_rows()
    compared: list[dict[str, Any]] = []
    all_current: list[float] = []
    for row in rows:
        raw = row["raw_json"] if isinstance(row.get("raw_json"), dict) else {}
        previous = raw.get("crm_evaluation_result_previous_log")
        current = raw.get("crm_evaluation_result_current")
        previous_profit = expected_profit(previous)
        current_profit = expected_profit(current)
        if current_profit is not None:
            all_current.append(current_profit)
        if previous_profit is None or current_profit is None:
            continue
        old_items = item_count(raw, "llm_backups.before_current_reprocess.llm_extracted_items")
        new_items = item_count(raw, "llm_extracted_items")
        compared.append(
            {
                "id": row["id"],
                "external_id": row["external_id"],
                "title": row["title"],
                "price": row["price"],
                "url": row["url"],
                "previous_profit": previous_profit,
                "current_profit": current_profit,
                "delta": round(current_profit - previous_profit, 2),
                "previous_decision": decision(previous),
                "current_decision": decision(current),
                "old_items_count": old_items,
                "new_items_count": new_items,
                "items_delta": (new_items - old_items) if isinstance(old_items, int) and isinstance(new_items, int) else None,
            }
        )

    deltas = [row["delta"] for row in compared]
    improved = [row for row in compared if row["delta"] > 0]
    worsened = [row for row in compared if row["delta"] < 0]
    unchanged = [row for row in compared if row["delta"] == 0]
    decision_changed = [row for row in compared if row["previous_decision"] != row["current_decision"]]
    became_good = [
        row for row in compared
        if row["previous_decision"] != "good" and row["current_decision"] == "good"
    ]
    stopped_good = [
        row for row in compared
        if row["previous_decision"] == "good" and row["current_decision"] != "good"
    ]

    report = {
        "total_listings": len(rows),
        "with_current_profit": len(all_current),
        "comparable_previous_current": len(compared),
        "improved_count": len(improved),
        "worsened_count": len(worsened),
        "unchanged_count": len(unchanged),
        "decision_changed_count": len(decision_changed),
        "became_good_count": len(became_good),
        "stopped_good_count": len(stopped_good),
        "delta_sum": round(sum(deltas), 2) if deltas else 0,
        "delta_avg": round(statistics.mean(deltas), 2) if deltas else None,
        "delta_median": round(statistics.median(deltas), 2) if deltas else None,
        "current_profit_positive_count": sum(1 for value in all_current if value > 0),
        "current_profit_checkable_count": sum(1 for value in all_current if value >= 300),
        "top_improved": sorted(compared, key=lambda row: row["delta"], reverse=True)[:30],
        "top_worsened": sorted(compared, key=lambda row: row["delta"])[:30],
        "decision_changed_examples": decision_changed[:80],
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2, default=str))
