from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.crm_market import (  # noqa: E402
    build_market_import_payload,
    build_market_listing_payload,
    evaluate_record_profit_if_configured,
    lot_cost_observations_from_items,
    market_price_observations_from_items,
    post_market_import,
)
from app.llm_analyzer import extract_listing_items_if_configured, interpret_listing_price_for_record  # noqa: E402
from app.main import (  # noqa: E402
    description_summary_threshold,
    listing_reservation_status,
    telegram_suppression_reason,
    truncate_description_for_telegram,
)
from app.pipeline import (  # noqa: E402
    build_saved_payload,
    load_automation_settings,
    monitor_json_to_record,
    save_record_to_db,
)


ITEMS_DIR = ROOT / "json_responses" / "avito_items"
STATE_PATH = ROOT / "data" / "parsed" / "llm_crm_backfill_state.json"
RESULTS_JSONL = ROOT / "data" / "parsed" / "llm_crm_backfill_results.jsonl"
CRM_IMPORT_URL = "http://127.0.0.1:8001/api/market/imports/avito"
CRM_EVALUATE_URL = "http://127.0.0.1:8001/api/market/evaluate-avito-listing"


def configure_crm_env() -> None:
    os.environ["CRM_MARKET_API_URL"] = CRM_IMPORT_URL
    os.environ["CRM_MARKET_EVALUATE_API_URL"] = CRM_EVALUATE_URL
    token = os.environ.get("MARKET_IMPORT_TOKEN")
    if token and not os.environ.get("CRM_MARKET_API_TOKEN"):
        os.environ["CRM_MARKET_API_TOKEN"] = token


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"processed": []}
    try:
        loaded = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"processed": []}
    return loaded if isinstance(loaded, dict) else {"processed": []}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def append_result(payload: dict[str, Any]) -> None:
    RESULTS_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS_JSONL.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def item_files() -> list[Path]:
    return sorted(ITEMS_DIR.glob("*.json"))


def process_file(path: Path, settings: dict[str, object], *, send_evaluate: bool) -> dict[str, Any]:
    details = json.loads(path.read_text(encoding="utf-8"))
    record = monitor_json_to_record(details)
    original_description = record.description

    reservation_status = (
        "reserved"
        if getattr(record, "is_reserved", None) is True
        else listing_reservation_status(record)
    )
    setattr(record, "reservation_status", reservation_status)

    llm_items = extract_listing_items_if_configured(
        record,
        settings,
        original_description=original_description,
    )
    if llm_items is not None:
        setattr(record, "llm_extracted_items", llm_items)

    record.description = truncate_description_for_telegram(
        record.description,
        threshold=description_summary_threshold(settings),
    )
    suppression_reason = telegram_suppression_reason(record)

    saved_payload = build_saved_payload(
        record,
        source_path=path,
        original_description=original_description,
        telegram_suppression_reason_value=suppression_reason,
        reservation_status=reservation_status,
    )
    saved_payload["llm_listing_price_interpretation"] = interpret_listing_price_for_record(record)
    saved_payload["llm_market_price_observations"] = market_price_observations_from_items(
        getattr(record, "llm_extracted_items", None)
    )
    saved_payload["llm_lot_cost_observations"] = lot_cost_observations_from_items(
        getattr(record, "llm_extracted_items", None)
    )

    db_result = save_record_to_db(saved_payload, source_path=path)
    evaluation_result = (
        evaluate_record_profit_if_configured(
            record,
            original_description=original_description,
            saved_payload=saved_payload,
        )
        if send_evaluate
        else {"ok": True, "skipped": True, "reason": "disabled"}
    )

    return {
        "path": str(path),
        "title": record.title,
        "url": record.url,
        "llm_items_count": len(getattr(record, "llm_extracted_items", []) or []),
        "db_result": {
            "inserted": db_result.inserted,
            "updated": db_result.updated,
            "skipped": db_result.skipped,
            "metrics": db_result.metrics,
        },
        "evaluation_result": evaluation_result,
        "saved_payload": saved_payload,
    }


def send_import_batch(processed: list[dict[str, Any]]) -> dict[str, Any]:
    listings = []
    crm_sent_at = datetime.now().astimezone().isoformat(timespec="seconds")
    for item in processed:
        payload = item.get("saved_payload")
        if not isinstance(payload, dict):
            continue
        fake = type("RecordProxy", (), {})()
        for key, value in payload.items():
            setattr(fake, key, value)
        setattr(fake, "source", "avito_playwright_parser")
        listing = build_market_listing_payload(
            fake,
            original_description=payload.get("original_description") if isinstance(payload.get("original_description"), str) else None,
            saved_payload=payload,
            crm_sent_at=crm_sent_at,
        )
        listings.append(listing)

    if not listings:
        return {"ok": True, "skipped": True, "reason": "no listings"}
    import_payload = build_market_import_payload(
        listings,
        filename="monitor_backfill_llm_crm",
        crm_sent_at=crm_sent_at,
        source="avito_playwright_parser",
    )
    return post_market_import(import_payload, timeout_seconds=60.0)


def run(*, limit: int | None, resume: bool, send_evaluate: bool, send_import: bool, workers: int = 1) -> dict[str, Any]:
    configure_crm_env()
    settings = load_automation_settings()
    files = item_files()
    state = load_state() if resume else {"processed": []}
    already = set(str(item) for item in state.get("processed", []) if item)
    pending = [path for path in files if str(path) not in already]
    if limit is not None:
        pending = pending[:limit]

    totals = {
        "seen_files": len(files),
        "already_processed": len(already),
        "pending_this_run": len(pending),
        "processed": 0,
        "errors": 0,
        "llm_items_total": 0,
        "db_inserted": 0,
        "db_updated": 0,
        "db_metrics": 0,
        "evaluation_ok": 0,
        "evaluation_failed": 0,
        "import_ok": 0,
        "import_failed": 0,
    }
    started = time.time()
    def handle_one(index_path: tuple[int, Path]) -> tuple[int, Path, dict[str, Any] | None, Exception | None]:
        index, path = index_path
        try:
            result = process_file(path, settings, send_evaluate=send_evaluate)
            import_result = {
                "ok": True,
                "skipped": True,
                "reason": "stored by save_record_to_db/postgresql",
            } if send_import else {
                "ok": True,
                "skipped": True,
                "reason": "disabled",
            }
            result["import_result"] = import_result
            return index, path, result, None
        except Exception as error:
            return index, path, None, error

    def consume(index: int, path: Path, result: dict[str, Any] | None, error: Exception | None) -> None:
        if error is not None or result is None:
            totals["errors"] += 1
            append_result({"path": str(path), "ok": False, "error": str(error)})
            print(f"[{index}/{len(pending)}] ERROR {path}: {error}", flush=True)
            return
        totals["processed"] += 1
        totals["llm_items_total"] += int(result["llm_items_count"])
        db_result = result["db_result"]
        totals["db_inserted"] += int(db_result["inserted"])
        totals["db_updated"] += int(db_result["updated"])
        totals["db_metrics"] += int(db_result["metrics"])
        eval_result = result["evaluation_result"]
        import_result = result["import_result"]
        if eval_result.get("ok"):
            totals["evaluation_ok"] += 1
        else:
            totals["evaluation_failed"] += 1
        if import_result.get("ok"):
            totals["import_ok"] += 1
        else:
            totals["import_failed"] += 1
        append_result({k: v for k, v in result.items() if k != "saved_payload"})
        already.add(str(path))
        state["processed"] = sorted(already)
        state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        save_state(state)
        print(
            f"[{index}/{len(pending)}] ok items={result['llm_items_count']} "
            f"eval_ok={eval_result.get('ok')} import_ok={import_result.get('ok')} "
            f"title={result.get('title')!r}",
            flush=True,
        )

    workers = max(1, min(int(workers or 1), 12))
    if workers == 1:
        for index_path in enumerate(pending, start=1):
            consume(*handle_one(index_path))
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(handle_one, index_path) for index_path in enumerate(pending, start=1)]
            for future in as_completed(futures):
                consume(*future.result())

    totals["duration_seconds"] = round(time.time() - started, 1)
    save_state(state)
    return totals


def main() -> int:
    parser = argparse.ArgumentParser(description="Run LLM extraction and CRM import/evaluation for collected cards.")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--no-evaluate", action="store_true")
    parser.add_argument("--no-import", action="store_true")
    parser.add_argument("--workers", type=int, default=1, help="Parallel LLM workers, 1-12")
    args = parser.parse_args()
    totals = run(
        limit=args.limit,
        resume=not args.no_resume,
        send_evaluate=not args.no_evaluate,
        send_import=not args.no_import,
        workers=args.workers,
    )
    print(json.dumps(totals, ensure_ascii=False, indent=2), flush=True)
    return 0 if totals["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
