from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.pipeline import build_saved_payload, monitor_json_to_record, save_record_to_db

AVITO_ITEMS_DIR = ROOT / "json_responses" / "avito_items"
JSON_RESPONSES_DIR = ROOT / "json_responses"


def iter_detail_payloads() -> list[tuple[Path, dict[str, Any]]]:
    payloads: list[tuple[Path, dict[str, Any]]] = []
    seen_paths: set[Path] = set()

    if AVITO_ITEMS_DIR.exists():
        for path in sorted(AVITO_ITEMS_DIR.glob("*.json")):
            seen_paths.add(path.resolve())
            data = load_json(path)
            if isinstance(data, dict):
                payloads.append((path, data))

    for batch_path in sorted(JSON_RESPONSES_DIR.glob("*avito_item_details.json")):
        batch = load_json(batch_path)
        if not isinstance(batch, dict):
            continue
        items = batch.get("items")
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            item_id = item.get("item_id") or item.get("id")
            if not item_id:
                url = str(item.get("canonical_url") or item.get("final_url") or item.get("requested_url") or "")
                item_id = url.rstrip("/").rsplit("_", 1)[-1] if "_" in url else str(index)
            standalone = AVITO_ITEMS_DIR / f"{item_id}.json"
            if standalone.resolve() in seen_paths:
                continue
            payloads.append((batch_path, item))

    return payloads


def load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def backfill(*, dry_run: bool = False) -> dict[str, int]:
    stats = {
        "files_seen": 0,
        "inserted": 0,
        "updated": 0,
        "skipped": 0,
        "metrics": 0,
        "errors": 0,
    }
    for source_path, details in iter_detail_payloads():
        stats["files_seen"] += 1
        try:
            record = monitor_json_to_record(details)
            payload = build_saved_payload(record, source_path=source_path, original_description=record.description)
            if dry_run:
                continue
            result = save_record_to_db(payload, source_path=source_path)
            stats["inserted"] += result.inserted
            stats["updated"] += result.updated
            stats["skipped"] += result.skipped
            stats["metrics"] += result.metrics
        except Exception as error:
            stats["errors"] += 1
            print(f"[ERROR] {source_path}: {error}")
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill collected Monitor Avito JSON files into SQLite.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    stats = backfill(dry_run=args.dry_run)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
