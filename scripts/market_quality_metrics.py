from __future__ import annotations

import argparse
import csv
import json
import os
import re
from decimal import Decimal
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = PROJECT_ROOT / "reports"
DEFAULT_ENV_PATHS = [
    PROJECT_ROOT / ".env",
    PROJECT_ROOT / "crm" / ".env",
    Path(r"C:\crm_inventory\.env"),
]

BAD_IMAGE_TOKENS = ("static/", "sale-banner", "banner", "alfa", "logo", "icon", "avatar")
LISTING_IMAGE_RE = re.compile(r"^https?://\d+\.img\.avito\.st/image/", re.IGNORECASE)


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
    for key in ("DATABASE_URL", "CRM_DATABASE_URL"):
        value = os.getenv(key)
        if value:
            return normalize_postgres_url(value)
    for path in DEFAULT_ENV_PATHS:
        values = load_env_file(path)
        value = values.get("DATABASE_URL") or values.get("CRM_DATABASE_URL")
        if value:
            return normalize_postgres_url(value)
    raise SystemExit("DATABASE_URL not found. Expected it in .env or C:\\crm_inventory\\.env")


def normalize_postgres_url(value: str) -> str:
    return value.replace("postgresql+psycopg://", "postgresql://", 1)


def connect():
    try:
        import psycopg
    except ImportError as exc:
        raise SystemExit(
            "psycopg is required. Run with C:\\crm_inventory\\.venv\\Scripts\\python.exe "
            "or install psycopg in this environment."
        ) from exc
    return psycopg.connect(database_url())


def scalar(cur, sql: str, params: tuple[Any, ...] = ()) -> int | float:
    cur.execute(sql, params)
    value = cur.fetchone()[0]
    return value or 0


def fetch_dicts(cur, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    cur.execute(sql, params)
    names = [column.name for column in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def pct(part: int | float, total: int | float) -> float:
    if not total:
        return 0.0
    return round(float(part) * 100.0 / float(total), 2)


def json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def is_bad_image_url(url: str) -> bool:
    lowered = str(url or "").lower()
    if not LISTING_IMAGE_RE.match(str(url or "").strip()):
        return True
    return any(token in lowered for token in BAD_IMAGE_TOKENS)


def automatic_metrics() -> dict[str, Any]:
    with connect() as conn:
        cur = conn.cursor()
        total_listings = int(scalar(cur, "select count(*) from market_listings"))
        recent_24h = int(
            scalar(
                cur,
                "select count(*) from market_listings where scraped_at >= now() - interval '24 hours' "
                "or first_seen_at >= now() - interval '24 hours'",
            )
        )
        with_llm = int(
            scalar(
                cur,
                """
                select count(*) from market_listings
                where jsonb_typeof(raw_json::jsonb->'llm_extracted_items') = 'array'
                  and jsonb_array_length(raw_json::jsonb->'llm_extracted_items') > 0
                """,
            )
        )
        llm_empty = int(
            scalar(
                cur,
                """
                select count(*) from market_listings
                where coalesce(jsonb_array_length(
                    case when jsonb_typeof(raw_json::jsonb->'llm_extracted_items') = 'array'
                         then raw_json::jsonb->'llm_extracted_items'
                         else '[]'::jsonb end
                ), 0) = 0
                """,
            )
        )
        llm_errors = int(
            scalar(
                cur,
                """
                select count(*) from market_listings
                where raw_json::jsonb ? 'llm_error'
                  and raw_json::jsonb->'llm_error' is not null
                  and raw_json::jsonb->>'llm_error' <> 'null'
                """,
            )
        )
        llm_items_total = int(
            scalar(
                cur,
                """
                select count(*)
                from market_listings m
                cross join lateral jsonb_array_elements(
                    case when jsonb_typeof(m.raw_json::jsonb->'llm_extracted_items') = 'array'
                         then m.raw_json::jsonb->'llm_extracted_items'
                         else '[]'::jsonb end
                ) item
                """,
            )
        )
        by_route = fetch_dicts(
            cur,
            """
            select coalesce(raw_json::jsonb->>'llm_route', 'unknown') as route, count(*) as count
            from market_listings
            group by 1
            order by 2 desc
            """,
        )
        evaluated_listings = int(
            scalar(
                cur,
                """
                select count(*) from market_listings
                where jsonb_typeof(raw_json::jsonb->'crm_evaluation_result') = 'object'
                """,
            )
        )
        evaluation_items_total = int(
            scalar(
                cur,
                """
                select count(*)
                from market_listings m
                cross join lateral jsonb_array_elements(
                    case when jsonb_typeof(m.raw_json::jsonb#>'{crm_evaluation_result,items}') = 'array'
                         then m.raw_json::jsonb#>'{crm_evaluation_result,items}'
                         else '[]'::jsonb end
                ) item
                """,
            )
        )
        unpriced_items = int(
            scalar(
                cur,
                """
                select count(*)
                from market_listings m
                cross join lateral jsonb_array_elements(
                    case when jsonb_typeof(m.raw_json::jsonb#>'{crm_evaluation_result,items}') = 'array'
                         then m.raw_json::jsonb#>'{crm_evaluation_result,items}'
                         else '[]'::jsonb end
                ) item
                where item->>'expected_sell_price' is null
                   or item->>'expected_sell_price' = ''
                   or item->>'matched_catalog_name' is null
                """,
            )
        )
        decisions = fetch_dicts(
            cur,
            """
            select coalesce(raw_json::jsonb#>>'{crm_evaluation_result,decision}', 'not_saved') as decision, count(*) as count
            from market_listings
            group by 1
            order by 2 desc
            """,
        )
        profit_rows = fetch_dicts(
            cur,
            """
            select
                count(*) filter (where (raw_json::jsonb#>>'{crm_evaluation_result,total,expected_profit}')::numeric > 0) as positive,
                count(*) filter (where (raw_json::jsonb#>>'{crm_evaluation_result,total,expected_profit}')::numeric <= 0) as non_positive,
                avg((raw_json::jsonb#>>'{crm_evaluation_result,total,expected_profit}')::numeric) as avg_profit
            from market_listings
            where raw_json::jsonb#>>'{crm_evaluation_result,total,expected_profit}' is not null
            """,
        )[0]
        price_observations_total = int(scalar(cur, "select count(*) from price_observations"))
        price_observations_usable = int(scalar(cur, "select count(*) from price_observations where usable_for_auto_price"))
        price_by_type = fetch_dicts(
            cur,
            """
            select observation_type, count(*) as count
            from price_observations
            group by observation_type
            order by count(*) desc
            """,
        )
        image_rows = fetch_dicts(
            cur,
            """
            select
                external_id,
                title,
                coalesce(
                    raw_json::jsonb->'image_urls',
                    raw_json::jsonb#>'{link_monitor_payload,image_urls}'
                ) as image_urls
            from market_listings
            where jsonb_typeof(coalesce(
                    raw_json::jsonb->'image_urls',
                    raw_json::jsonb#>'{link_monitor_payload,image_urls}'
                )) = 'array'
            order by id desc
            limit 500
            """,
        )
        listings_with_images = 0
        image_urls_total = 0
        bad_image_urls = 0
        bad_image_examples: list[dict[str, Any]] = []
        for row in image_rows:
            urls = row.get("image_urls") or []
            if isinstance(urls, str):
                try:
                    urls = json.loads(urls)
                except json.JSONDecodeError:
                    urls = []
            if not isinstance(urls, list) or not urls:
                continue
            listings_with_images += 1
            image_urls_total += len(urls)
            bad = [url for url in urls if is_bad_image_url(str(url))]
            bad_image_urls += len(bad)
            if bad and len(bad_image_examples) < 10:
                bad_image_examples.append(
                    {
                        "external_id": row.get("external_id"),
                        "title": row.get("title"),
                        "bad_urls": bad[:3],
                    }
                )

    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "listings": {
            "total": total_listings,
            "recent_24h": recent_24h,
        },
        "llm": {
            "listings_with_items": with_llm,
            "listings_without_items": llm_empty,
            "llm_empty_rate_percent": pct(llm_empty, total_listings),
            "llm_error_count": llm_errors,
            "llm_error_rate_percent": pct(llm_errors, total_listings),
            "extracted_items_total": llm_items_total,
            "routes": by_route,
            "miss_rate_percent": "requires_manual_labels",
            "hallucination_rate_percent": "requires_manual_labels",
        },
        "crm": {
            "evaluated_listings_saved": evaluated_listings,
            "evaluation_items_total": evaluation_items_total,
            "unpriced_or_unmatched_items": unpriced_items,
            "quarantine_proxy_rate_percent": pct(unpriced_items, evaluation_items_total),
            "decisions": decisions,
            "profit": profit_rows,
        },
        "auto_prices": {
            "price_observations_total": price_observations_total,
            "price_observations_usable": price_observations_usable,
            "usable_rate_percent": pct(price_observations_usable, price_observations_total),
            "by_type": price_by_type,
        },
        "telegram_images": {
            "sampled_listings_with_images": listings_with_images,
            "sampled_image_urls_total": image_urls_total,
            "bad_image_urls_in_sample": bad_image_urls,
            "bad_image_rate_percent": pct(bad_image_urls, image_urls_total),
            "bad_image_examples": bad_image_examples,
        },
        "profit_realization": {
            "status": "requires_linking_bot_prediction_to_closed_sale",
            "formula": "actual_net_profit / expected_profit_at_parse * 100",
        },
        "price_variance": {
            "status": "requires_closed_sale_pairing_or_manual_item_mapping",
            "formula": "actual_sale_price / crm_auto_price - 1",
        },
    }


def write_sample_csv(limit: int) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"llm_quality_manual_sample_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    with connect() as conn:
        cur = conn.cursor()
        rows = fetch_dicts(
            cur,
            """
            select external_id, title, price, address, description, url, raw_json::jsonb->'llm_extracted_items' as llm_items
            from market_listings
            where description is not null
            order by random()
            limit %s
            """,
            (limit,),
        )
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "external_id",
                "title",
                "price",
                "address",
                "url",
                "description",
                "llm_items_json",
                "expected_items_manual",
                "missed_count_manual",
                "hallucinated_count_manual",
                "notes",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "external_id": row.get("external_id"),
                    "title": row.get("title"),
                    "price": row.get("price"),
                    "address": row.get("address"),
                    "url": row.get("url"),
                    "description": row.get("description"),
                    "llm_items_json": json.dumps(row.get("llm_items") or [], ensure_ascii=False),
                    "expected_items_manual": "",
                    "missed_count_manual": "",
                    "hallucinated_count_manual": "",
                    "notes": "",
                }
            )
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Avito bot LLM/CRM/image quality metrics from CRM PostgreSQL.")
    parser.add_argument("--output", default=None, help="JSON report path")
    parser.add_argument("--manual-sample", type=int, default=0, help="Write CSV sample for manual LLM quality labeling")
    args = parser.parse_args()

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    metrics = automatic_metrics()
    output = Path(args.output) if args.output else REPORTS_DIR / f"market_quality_metrics_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    output.write_text(json.dumps(metrics, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")
    print(f"[METRICS] JSON: {output}")
    print(json.dumps(metrics, ensure_ascii=False, indent=2, default=json_default))
    if args.manual_sample:
        sample = write_sample_csv(args.manual_sample)
        print(f"[METRICS] Manual LLM sample CSV: {sample}")


if __name__ == "__main__":
    main()
