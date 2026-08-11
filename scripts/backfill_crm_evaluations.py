from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_PATHS = [
    PROJECT_ROOT / ".env",
    PROJECT_ROOT / "crm" / ".env",
    Path(r"C:\crm_inventory\.env"),
]
DEFAULT_EVALUATE_URL = "http://127.0.0.1:8001/api/market/evaluate-avito-listing"


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def setting(name: str, default: str | None = None) -> str | None:
    if os.getenv(name):
        return os.getenv(name)
    for path in DEFAULT_ENV_PATHS:
        values = load_env_file(path)
        if values.get(name):
            return values[name]
    return default


def database_url() -> str:
    value = setting("DATABASE_URL") or setting("CRM_DATABASE_URL")
    if not value:
        raise SystemExit("DATABASE_URL not found")
    return value.replace("postgresql+psycopg://", "postgresql://", 1)


def connect():
    try:
        import psycopg
    except ImportError as exc:
        raise SystemExit("Run with C:\\crm_inventory\\.venv\\Scripts\\python.exe or install psycopg") from exc
    return psycopg.connect(database_url())


def post_json(url: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def evaluation_payload(row: dict[str, Any]) -> dict[str, Any]:
    raw_json = row.get("raw_json") if isinstance(row.get("raw_json"), dict) else {}
    return {
        "listing": {
            "title": row.get("title"),
            "price": row.get("price"),
            "delivery_price_rub": raw_json.get("delivery_price_rub"),
            "raw_json": raw_json,
        },
        "extracted_items": raw_json.get("llm_extracted_items") or [],
    }


def fetch_rows(conn, *, limit: int, force: bool) -> list[dict[str, Any]]:
    sql = """
        select id, external_id, title, price, raw_json
        from market_listings
        where jsonb_typeof(raw_json::jsonb->'llm_extracted_items') = 'array'
          and jsonb_array_length(raw_json::jsonb->'llm_extracted_items') > 0
    """
    if not force:
        sql += """
          and not (
            raw_json::jsonb ? 'crm_evaluation_result'
            and jsonb_typeof(raw_json::jsonb->'crm_evaluation_result') = 'object'
          )
        """
    sql += " order by id"
    params: tuple[Any, ...] = ()
    if limit > 0:
        sql += " limit %s"
        params = (limit,)
    cur = conn.cursor()
    cur.execute(sql, params)
    names = [column.name for column in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def update_row(conn, row_id: int, raw_json: dict[str, Any]) -> None:
    from psycopg.types.json import Jsonb

    conn.execute("update market_listings set raw_json = %s where id = %s", (Jsonb(raw_json), row_id))


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill CRM evaluation result into market_listings.raw_json.")
    parser.add_argument("--url", default=setting("CRM_MARKET_EVALUATE_API_URL", DEFAULT_EVALUATE_URL))
    parser.add_argument("--limit", type=int, default=0, help="0 means all")
    parser.add_argument("--force", action="store_true", help="Recalculate rows that already have crm_evaluation_result")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    done = 0
    failed = 0
    with connect() as conn:
        rows = fetch_rows(conn, limit=args.limit, force=args.force)
        print(f"[BACKFILL] rows={len(rows)} force={args.force}")
        for index, row in enumerate(rows, start=1):
            try:
                response = post_json(args.url, evaluation_payload(row), timeout=args.timeout)
                raw_json = row["raw_json"] if isinstance(row.get("raw_json"), dict) else {}
                raw_json["crm_evaluation_result"] = response
                raw_json["crm_evaluation_saved_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                update_row(conn, int(row["id"]), raw_json)
                done += 1
                if done % 50 == 0:
                    conn.commit()
                    print(f"[BACKFILL] done={done}/{len(rows)} failed={failed}")
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                failed += 1
                print(f"[BACKFILL] failed id={row.get('id')} external={row.get('external_id')}: {type(exc).__name__}: {exc}")
        conn.commit()
    print(f"[BACKFILL] complete done={done} failed={failed}")


if __name__ == "__main__":
    main()
