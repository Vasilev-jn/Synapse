from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.pipeline import extract_avito_state_from_html_file  # noqa: E402


DB_DSN = os.environ.get("CRM_POSTGRES_DSN") or "postgresql://postgres:postgres@127.0.0.1:5432/crm_inventory"
SNAPSHOTS_DIR = ROOT / "json_responses" / "page_snapshots"


def latest_snapshot_paths() -> dict[str, Path]:
    result: dict[str, Path] = {}
    if not SNAPSHOTS_DIR.exists():
        return result
    for item_dir in SNAPSHOTS_DIR.iterdir():
        if not item_dir.is_dir():
            continue
        html_files = sorted(item_dir.glob("*.html"), key=lambda path: path.stat().st_mtime)
        if html_files:
            result[item_dir.name] = html_files[-1]
    return result


def load_listing(conn: psycopg.Connection, external_id: str) -> dict[str, Any] | None:
    return conn.execute(
        """
        select id, external_id, scraped_at, raw_json::jsonb as raw_json
        from market_listings
        where external_id = %s
        """,
        (external_id,),
    ).fetchone()


def run(*, dry_run: bool = False) -> dict[str, int]:
    stats = {
        "snapshots": 0,
        "matched_listings": 0,
        "with_posted_at": 0,
        "with_reserved": 0,
        "updated": 0,
    }
    snapshots = latest_snapshot_paths()
    stats["snapshots"] = len(snapshots)
    with psycopg.connect(DB_DSN, row_factory=dict_row) as conn:
        for external_id, path in snapshots.items():
            listing = load_listing(conn, external_id)
            if not listing:
                continue
            stats["matched_listings"] += 1
            collected_at = listing["scraped_at"].isoformat() if listing.get("scraped_at") else None
            state = extract_avito_state_from_html_file(path, collected_at)
            if state.get("posted_at"):
                stats["with_posted_at"] += 1
            if state.get("is_reserved") is not None:
                stats["with_reserved"] += 1
            raw_json = listing["raw_json"] if isinstance(listing["raw_json"], dict) else {}
            raw_json["avito_state"] = state
            raw_json["posted_at"] = state.get("posted_at")
            raw_json["posted_at_text"] = state.get("posted_at_text")
            raw_json["avito_server_date"] = state.get("server_date")
            raw_json["is_reserved"] = state.get("is_reserved")
            raw_json["is_active"] = state.get("is_active")
            raw_json["finish_time"] = state.get("finish_time")
            if state.get("reservation_status"):
                raw_json["reservation_status"] = state.get("reservation_status")
            if not dry_run:
                conn.execute(
                    """
                    update market_listings
                    set posted_at = coalesce(%s::timestamp, posted_at),
                        raw_json = %s
                    where id = %s
                    """,
                    (state.get("posted_at"), Json(raw_json), listing["id"]),
                )
            stats["updated"] += 1
        if not dry_run:
            conn.commit()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill Avito posted/reserved state from saved HTML snapshots.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(dry_run=args.dry_run), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
