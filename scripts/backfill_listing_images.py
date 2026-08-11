from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ITEMS_DIR = PROJECT_ROOT / "json_responses" / "avito_items"
DEFAULT_ENV_PATHS = [PROJECT_ROOT / ".env", PROJECT_ROOT / "crm" / ".env", Path(r"C:\crm_inventory\.env")]
AVITO_LISTING_IMAGE_RE = re.compile(r"^https?://\d+\.img\.avito\.st/image/", re.IGNORECASE)
IMAGE_BLOCKLIST_TOKENS = ("static/", "sale-banner", "banner", "alfa", "logo", "icon", "avatar")


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


def database_url() -> str:
    value = os.getenv("DATABASE_URL") or os.getenv("CRM_DATABASE_URL")
    if value:
        return value.replace("postgresql+psycopg://", "postgresql://", 1)
    for path in DEFAULT_ENV_PATHS:
        values = load_env_file(path)
        value = values.get("DATABASE_URL") or values.get("CRM_DATABASE_URL")
        if value:
            return value.replace("postgresql+psycopg://", "postgresql://", 1)
    raise SystemExit("DATABASE_URL not found")


def connect():
    try:
        import psycopg
    except ImportError as exc:
        raise SystemExit("Run with C:\\crm_inventory\\.venv\\Scripts\\python.exe or install psycopg") from exc
    return psycopg.connect(database_url())


def is_avito_listing_image_url(url: str) -> bool:
    normalized = str(url or "").strip()
    lowered = normalized.lower()
    if not AVITO_LISTING_IMAGE_RE.match(normalized):
        return False
    return not any(token in lowered for token in IMAGE_BLOCKLIST_TOKENS)


def filtered_urls(values: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    candidates = values if isinstance(values, list) else []
    for value in candidates:
        url = str(value or "").strip()
        if not is_avito_listing_image_url(url) or url in seen:
            continue
        seen.add(url)
        result.append(url)
    return result


def load_file_images(items_dir: Path) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for path in items_dir.glob("*.json"):
        item_id = path.stem
        if not item_id.isdigit():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        urls = filtered_urls(data.get("image_urls"))
        if urls:
            result[item_id] = urls
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill filtered Avito image_urls from json_responses/avito_items into CRM PostgreSQL.")
    parser.add_argument("--items-dir", default=str(ITEMS_DIR))
    args = parser.parse_args()

    images_by_id = load_file_images(Path(args.items_dir))
    print(f"[IMAGES] file listings with images={len(images_by_id)}")
    updated = 0
    from psycopg.types.json import Jsonb

    with connect() as conn:
        for external_id, urls in images_by_id.items():
            row = conn.execute(
                "select id, raw_json from market_listings where external_id = %s",
                (external_id,),
            ).fetchone()
            if not row:
                continue
            row_id, raw_json = row
            raw = raw_json if isinstance(raw_json, dict) else {}
            if raw.get("image_urls") == urls:
                continue
            raw["image_urls"] = urls
            conn.execute("update market_listings set raw_json = %s where id = %s", (Jsonb(raw), row_id))
            updated += 1
        conn.commit()
    print(f"[IMAGES] updated={updated}")


if __name__ == "__main__":
    main()
