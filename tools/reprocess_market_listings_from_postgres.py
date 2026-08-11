from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime
import http.client
import json
import os
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import urlsplit

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json


ROOT = Path(__file__).resolve().parents[1]
CRM_ROOT = Path("C:/crm_inventory")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.crm_market import (  # noqa: E402
    build_market_import_payload,
    lot_cost_observations_from_items,
    market_price_observations_from_items,
)
from app.llm_analyzer import (  # noqa: E402
    extract_listing_items_if_configured,
    interpret_listing_price_for_record,
)
from app.monitor import NewListingRecord, seller_city_from_address  # noqa: E402
from app.pipeline import infer_item_kind, load_automation_settings  # noqa: E402


DB_DSN = os.environ.get("CRM_POSTGRES_DSN") or "postgresql://postgres:postgres@127.0.0.1:5432/crm_inventory"
CRM_BASE_URL = os.environ.get("CRM_BASE_URL") or "http://127.0.0.1:8001"
CRM_IMPORT_URL = f"{CRM_BASE_URL.rstrip('/')}/api/market/imports/avito"
CRM_EVALUATE_URL = f"{CRM_BASE_URL.rstrip('/')}/api/market/evaluate-avito-listing"
RESULTS_DIR = ROOT / "reports"
BACKFILL_LOG = ROOT / "data" / "parsed" / "llm_crm_backfill_results.jsonl"
RUN_ID_PREFIX = "postgres_llm_reprocess"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def avito_external_id(url: str | None) -> str | None:
    if not url:
        return None
    match = re.search(r"_(\d{6,})(?:[/?#]|$)", url)
    if match:
        return match.group(1)
    match = re.search(r"/(\d{6,})(?:[/?#]|$)", url)
    return match.group(1) if match else None


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value).replace("\xa0", " ")).strip()
    return text or None


def list_market_listings(
    limit: int | None = None,
    only_missing_run: str | None = None,
    only_missing_current_eval: bool = False,
) -> list[dict[str, Any]]:
    where = ""
    params: dict[str, Any] = {}
    clauses: list[str] = []
    if only_missing_run:
        clauses.append("not (raw_json::jsonb #> '{analysis_runs}' ? %(run_id)s)")
        params["run_id"] = only_missing_run
    if only_missing_current_eval:
        clauses.append("not (raw_json::jsonb ? 'crm_evaluation_result_current')")
    if clauses:
        where = "where " + " and ".join(clauses)
    sql = f"""
        select id, external_id, title, description, url, price, currency, address,
               platform, format, type, posted_at, scraped_at, raw_json::jsonb as raw_json
        from market_listings
        {where}
        order by id
    """
    if limit:
        sql += " limit %(limit)s"
        params["limit"] = limit
    with psycopg.connect(DB_DSN, row_factory=dict_row) as conn:
        return list(conn.execute(sql, params))


def load_previous_evaluations() -> dict[str, dict[str, Any]]:
    previous: dict[str, dict[str, Any]] = {}
    if not BACKFILL_LOG.exists():
        return previous
    with BACKFILL_LOG.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            url = clean_text(row.get("url"))
            external_id = avito_external_id(url)
            eval_result = row.get("evaluation_result")
            if not external_id or not isinstance(eval_result, dict):
                continue
            response = eval_result.get("response")
            if isinstance(response, dict):
                previous[external_id] = response
    return previous


def record_from_listing(row: dict[str, Any]) -> NewListingRecord:
    raw = row.get("raw_json") if isinstance(row.get("raw_json"), dict) else {}
    raw_card_texts = raw.get("raw_card_texts") if isinstance(raw.get("raw_card_texts"), list) else []
    raw_detail_texts = raw.get("raw_detail_texts") if isinstance(raw.get("raw_detail_texts"), list) else []
    description = clean_text(row.get("description") or raw.get("original_description"))
    title = clean_text(row.get("title"))
    address = clean_text(row.get("address"))
    record = NewListingRecord(
        saved_at=clean_text(raw.get("bot_saved_at") or raw.get("saved_at") or row.get("scraped_at")) or now_iso(),
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
    setattr(record, "source", clean_text(raw.get("source")) or "avito_playwright_parser")
    setattr(record, "external_id", clean_text(row.get("external_id")) or avito_external_id(record.url))
    if isinstance(raw.get("monitor_context"), dict):
        setattr(record, "monitor_context", raw["monitor_context"])
    reservation = raw.get("reservation_status")
    if reservation is not None:
        setattr(record, "reservation_status", reservation)
    return record


def listing_payload_from_row(row: dict[str, Any], raw_json: dict[str, Any]) -> dict[str, Any]:
    return {
        "external_id": row.get("external_id") or avito_external_id(row.get("url")),
        "title": row.get("title"),
        "description": row.get("description"),
        "url": row.get("url"),
        "price": row.get("price"),
        "currency": row.get("currency") or "RUB",
        "address": row.get("address"),
        "platform": row.get("platform"),
        "format": row.get("format"),
        "type": row.get("type"),
        "localization": None,
        "seller_name": None,
        "seller_user_key": None,
        "seller_type": None,
        "is_shop": False,
        "posted_at": row.get("posted_at").isoformat() if hasattr(row.get("posted_at"), "isoformat") else row.get("posted_at"),
        "scraped_at": row.get("scraped_at").isoformat() if hasattr(row.get("scraped_at"), "isoformat") else row.get("scraped_at"),
        "raw_json": raw_json,
    }


def post_json(url: str, payload: dict[str, Any], timeout_seconds: float = 60.0) -> dict[str, Any]:
    parsed = urlsplit(url)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("MARKET_IMPORT_TOKEN") or os.environ.get("CRM_MARKET_API_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    conn = http.client.HTTPConnection(parsed.hostname or "127.0.0.1", parsed.port or 80, timeout=timeout_seconds)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    try:
        conn.request("POST", path, body=body, headers=headers)
        response = conn.getresponse()
        text = response.read().decode("utf-8", errors="replace")
        data = json.loads(text) if text.strip() else {}
        return {"ok": 200 <= response.status < 300, "status": response.status, "response": data}
    except Exception as error:  # noqa: BLE001
        return {"ok": False, "error": str(error)}
    finally:
        conn.close()


def update_listing_raw_json(listing_id: int, raw_json: dict[str, Any]) -> None:
    with psycopg.connect(DB_DSN) as conn:
        conn.execute(
            "update market_listings set raw_json = %s where id = %s",
            (Json(raw_json), listing_id),
        )
        conn.commit()


def detect_old_card_signals(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("raw_json") if isinstance(row.get("raw_json"), dict) else {}
    haystack = " ".join(
        str(part or "")
        for part in [
            row.get("title"),
            row.get("description"),
            raw.get("delivery_text"),
            raw.get("reservation_status"),
            " ".join(raw.get("raw_card_texts") if isinstance(raw.get("raw_card_texts"), list) else []),
            " ".join(raw.get("raw_detail_texts") if isinstance(raw.get("raw_detail_texts"), list) else []),
        ]
    ).lower().replace("ё", "е")
    return {
        "posted_at_column_present": row.get("posted_at") is not None,
        "reserved_text_present": bool(re.search(r"забронир|брон|reserved|booked", haystack, re.IGNORECASE)),
        "posted_text_present": bool(re.search(r"опубликован|размещен|размещено|сегодня|вчера", haystack, re.IGNORECASE)),
    }


def profit_value(evaluation: dict[str, Any] | None) -> float | None:
    if not isinstance(evaluation, dict):
        return None
    total = evaluation.get("total")
    if isinstance(total, dict) and isinstance(total.get("expected_profit"), (int, float)):
        return float(total["expected_profit"])
    return None


def process_listing(
    row: dict[str, Any],
    settings: dict[str, object],
    previous_evaluations: dict[str, dict[str, Any]],
    *,
    run_id: str,
    dry_run: bool,
) -> dict[str, Any]:
    record = record_from_listing(row)
    old_raw = row.get("raw_json") if isinstance(row.get("raw_json"), dict) else {}
    raw_json = dict(old_raw)
    previous_eval = previous_evaluations.get(str(row.get("external_id") or ""))

    old_items = raw_json.get("llm_extracted_items")
    new_items = extract_listing_items_if_configured(record, settings, original_description=record.description)
    if new_items is None:
        new_items = old_items if isinstance(old_items, list) else []
    setattr(record, "llm_extracted_items", new_items)

    raw_json.setdefault("llm_backups", {})
    if isinstance(raw_json["llm_backups"], dict) and "before_current_reprocess" not in raw_json["llm_backups"]:
        raw_json["llm_backups"]["before_current_reprocess"] = {
            "saved_at": now_iso(),
            "llm_extracted_items": old_items,
            "llm_listing_price_interpretation": raw_json.get("llm_listing_price_interpretation"),
            "llm_market_price_observations": raw_json.get("llm_market_price_observations"),
            "llm_lot_cost_observations": raw_json.get("llm_lot_cost_observations"),
        }

    raw_json["llm_extracted_items"] = new_items
    raw_json["llm_listing_price_interpretation"] = interpret_listing_price_for_record(record)
    raw_json["llm_market_price_observations"] = market_price_observations_from_items(new_items)
    raw_json["llm_lot_cost_observations"] = lot_cost_observations_from_items(new_items)
    raw_json["llm_reprocessed_at"] = now_iso()
    raw_json["llm_reprocess_run_id"] = run_id

    import_payload = build_market_import_payload(
        [listing_payload_from_row(row, raw_json)],
        filename=f"{run_id}_postgres",
        crm_sent_at=now_iso(),
        source=str(raw_json.get("source") or "avito_playwright_parser"),
    )
    import_result = {"ok": True, "skipped": True, "reason": "dry_run"}
    if not dry_run:
        import_result = post_json(CRM_IMPORT_URL, import_payload, timeout_seconds=90.0)

    evaluation_payload = {
        "listing": listing_payload_from_row(row, raw_json),
        "extracted_items": new_items,
    }
    evaluation_result = {"ok": True, "skipped": True, "reason": "dry_run"}
    new_eval_response: dict[str, Any] | None = None
    if not dry_run:
        evaluation_result = post_json(CRM_EVALUATE_URL, evaluation_payload, timeout_seconds=90.0)
        if isinstance(evaluation_result.get("response"), dict):
            new_eval_response = evaluation_result["response"]
            raw_json["crm_evaluation_result_current"] = new_eval_response
    elif previous_eval:
        new_eval_response = None

    raw_json["crm_evaluation_result_previous_log"] = previous_eval
    raw_json.setdefault("analysis_runs", {})
    if isinstance(raw_json["analysis_runs"], dict):
        raw_json["analysis_runs"][run_id] = {
            "processed_at": now_iso(),
            "old_items_count": len(old_items) if isinstance(old_items, list) else None,
            "new_items_count": len(new_items) if isinstance(new_items, list) else None,
            "previous_profit": profit_value(previous_eval),
            "current_profit": profit_value(new_eval_response),
            "current_decision": new_eval_response.get("decision") if isinstance(new_eval_response, dict) else None,
            "import_ok": import_result.get("ok"),
            "evaluation_ok": evaluation_result.get("ok"),
            "signals": detect_old_card_signals(row),
        }
    if not dry_run:
        update_listing_raw_json(int(row["id"]), raw_json)

    return {
        "listing_id": row["id"],
        "external_id": row.get("external_id"),
        "title": row.get("title"),
        "url": row.get("url"),
        "old_items_count": len(old_items) if isinstance(old_items, list) else None,
        "new_items_count": len(new_items) if isinstance(new_items, list) else None,
        "previous_profit": profit_value(previous_eval),
        "current_profit": profit_value(new_eval_response),
        "previous_decision": previous_eval.get("decision") if isinstance(previous_eval, dict) else None,
        "current_decision": new_eval_response.get("decision") if isinstance(new_eval_response, dict) else None,
        "import_ok": import_result.get("ok"),
        "evaluation_ok": evaluation_result.get("ok"),
        "import_error": import_result.get("error"),
        "evaluation_error": evaluation_result.get("error"),
        "signals": detect_old_card_signals(row),
    }


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def summarize(results: list[dict[str, Any]], *, started_at: str, finished_at: str, run_id: str) -> dict[str, Any]:
    comparable = [row for row in results if row.get("previous_profit") is not None and row.get("current_profit") is not None]
    improved = [
        row for row in comparable
        if (float(row["current_profit"]) - float(row["previous_profit"])) > 0
    ]
    worsened = [
        row for row in comparable
        if (float(row["current_profit"]) - float(row["previous_profit"])) < 0
    ]
    return {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "processed": len(results),
        "import_ok": sum(1 for row in results if row.get("import_ok")),
        "evaluation_ok": sum(1 for row in results if row.get("evaluation_ok")),
        "with_previous_profit": len([row for row in results if row.get("previous_profit") is not None]),
        "with_current_profit": len([row for row in results if row.get("current_profit") is not None]),
        "comparable": len(comparable),
        "profit_improved_count": len(improved),
        "profit_worsened_count": len(worsened),
        "profit_delta_total": round(
            sum(float(row["current_profit"]) - float(row["previous_profit"]) for row in comparable),
            2,
        ),
        "posted_at_column_present": sum(1 for row in results if row.get("signals", {}).get("posted_at_column_present")),
        "posted_text_present": sum(1 for row in results if row.get("signals", {}).get("posted_text_present")),
        "reserved_text_present": sum(1 for row in results if row.get("signals", {}).get("reserved_text_present")),
        "top_positive_delta": sorted(
            comparable,
            key=lambda row: float(row["current_profit"]) - float(row["previous_profit"]),
            reverse=True,
        )[:20],
        "top_negative_delta": sorted(
            comparable,
            key=lambda row: float(row["current_profit"]) - float(row["previous_profit"]),
        )[:20],
    }


def run(
    *,
    workers: int,
    limit: int | None,
    dry_run: bool,
    run_id: str | None,
    only_missing_current_eval: bool = False,
) -> dict[str, Any]:
    started_at = now_iso()
    run_id = run_id or f"{RUN_ID_PREFIX}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    settings = load_automation_settings()
    previous_evaluations = load_previous_evaluations()
    rows = list_market_listings(limit=limit, only_missing_current_eval=only_missing_current_eval)
    results_path = RESULTS_DIR / f"{run_id}_results.jsonl"
    summary_path = RESULTS_DIR / f"{run_id}_summary.json"
    workers = max(1, min(int(workers), 12))

    results: list[dict[str, Any]] = []
    print(
        f"[START] run_id={run_id} listings={len(rows)} workers={workers} dry_run={dry_run} "
        f"previous_eval_logs={len(previous_evaluations)}",
        flush=True,
    )

    def handle(row: dict[str, Any]) -> dict[str, Any]:
        return process_listing(row, settings, previous_evaluations, run_id=run_id, dry_run=dry_run)

    if workers == 1:
        iterator = ((index, handle(row)) for index, row in enumerate(rows, start=1))
        for index, result in iterator:
            results.append(result)
            append_jsonl(results_path, result)
            print_progress(index, len(rows), result)
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {executor.submit(handle, row): index for index, row in enumerate(rows, start=1)}
            completed = 0
            for future in as_completed(future_map):
                completed += 1
                result = future.result()
                results.append(result)
                append_jsonl(results_path, result)
                print_progress(completed, len(rows), result)

    summary = summarize(results, started_at=started_at, finished_at=now_iso(), run_id=run_id)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[DONE] {json.dumps({k: v for k, v in summary.items() if not k.startswith('top_')}, ensure_ascii=False)}", flush=True)
    print(f"[REPORT] {summary_path}", flush=True)
    return summary


def print_progress(index: int, total: int, result: dict[str, Any]) -> None:
    previous_profit = result.get("previous_profit")
    current_profit = result.get("current_profit")
    delta = None
    if previous_profit is not None and current_profit is not None:
        delta = round(float(current_profit) - float(previous_profit), 2)
    print(
        f"[{index}/{total}] id={result.get('listing_id')} items "
        f"{result.get('old_items_count')}->{result.get('new_items_count')} "
        f"profit {previous_profit}->{current_profit} delta={delta} "
        f"eval={result.get('evaluation_ok')} {result.get('title')!r}",
        flush=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-run LLM extraction and CRM profit evaluation for CRM PostgreSQL market_listings.")
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--only-missing-current-eval", action="store_true")
    args = parser.parse_args()
    run(
        workers=args.workers,
        limit=args.limit,
        dry_run=args.dry_run,
        run_id=args.run_id,
        only_missing_current_eval=args.only_missing_current_eval,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
