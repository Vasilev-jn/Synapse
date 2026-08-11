from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.llm_analyzer import (  # noqa: E402
    extract_listing_items_with_metadata,
    llm_api_key,
    llm_api_url,
    llm_heavy_fallback_model,
    llm_heavy_items_max_tokens,
    llm_heavy_items_timeout_seconds,
    llm_heavy_model,
    llm_items_max_tokens,
    llm_items_timeout_seconds,
    llm_model,
    llm_provider,
)
from app.monitor import NewListingRecord  # noqa: E402
from app.pipeline import load_automation_settings  # noqa: E402


DEFAULT_LISTING_PATH = ROOT / "data" / "parsed" / "link_monitor_latest.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe Qwen vs Luna Pro on one Avito listing text payload.")
    parser.add_argument("--listing-json", type=Path, default=DEFAULT_LISTING_PATH)
    parser.add_argument("--index", type=int, default=None, help="Optional index for JSON arrays.")
    parser.add_argument("--output", type=Path, default=ROOT / "reports" / "luna_probe_latest.json")
    parser.add_argument("--routes", default="light,heavy", help="Comma-separated routes to run: light,heavy.")
    parser.add_argument("--heavy-model", default=None, help="Override configured heavy model for this probe.")
    parser.add_argument("--heavy-timeout", type=int, default=None, help="Override heavy request timeout seconds.")
    parser.add_argument("--include-fallback", action="store_true", help="Also try heavy fallback model after flex.")
    args = parser.parse_args()

    settings = load_automation_settings()
    provider = llm_provider(settings)
    api_key = llm_api_key(provider)
    if not api_key:
        print(f"[PROBE] Missing API key for provider={provider}", file=sys.stderr)
        return 2

    payload = select_payload(load_json(args.listing_json), index=args.index)
    record = record_from_payload(payload)
    api_url = llm_api_url(provider)
    requested_routes = {route.strip().lower() for route in args.routes.split(",") if route.strip()}
    models = []
    if "light" in requested_routes:
        models.append(("light", llm_model(settings), llm_items_max_tokens(settings), llm_items_timeout_seconds(settings)))
    if "heavy" in requested_routes:
        models.append(
            (
                "heavy",
                args.heavy_model or llm_heavy_model(settings),
                llm_heavy_items_max_tokens(settings),
                args.heavy_timeout or llm_heavy_items_timeout_seconds(settings),
            )
        )
    if args.include_fallback:
        fallback_model = llm_heavy_fallback_model(settings)
        if fallback_model:
            models.append(("heavy_fallback", fallback_model, llm_heavy_items_max_tokens(settings), llm_heavy_items_timeout_seconds(settings)))

    results: list[dict[str, Any]] = []
    for route, model, max_tokens, timeout_seconds in models:
        print(f"[PROBE] route={route} model={model} timeout={timeout_seconds}s")
        result = extract_listing_items_with_metadata(
            record,
            api_key=api_key,
            model=model,
            api_url=api_url,
            original_description=record.description,
            max_tokens=max_tokens,
            request_timeout_seconds=timeout_seconds,
        )
        items = result.get("items") if isinstance(result.get("items"), list) else []
        results.append(
            {
                "route": route,
                "requested_model": model,
                "ok": bool(result.get("ok")),
                "response_model": result.get("response_model"),
                "elapsed_seconds": result.get("elapsed_seconds"),
                "request_timeout_seconds": result.get("request_timeout_seconds") or timeout_seconds,
                "items_count": len(items),
                "items_preview": items[:30],
                "error": result.get("error"),
            }
        )
        print(f"[PROBE] ok={bool(result.get('ok'))} items={len(items)} elapsed={result.get('elapsed_seconds')}")

    report = {
        "source": str(args.listing_json),
        "title": record.title,
        "url": record.url,
        "description_chars": len(record.description or ""),
        "raw_detail_texts_count": len(record.raw_detail_texts or []),
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[PROBE] Saved: {args.output}")
    return 0


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def select_payload(payload: Any, *, index: int | None = None) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        if index is not None:
            item = payload[index]
            if isinstance(item, dict):
                return item
            raise ValueError(f"listing JSON item at index {index} is not an object")
        for item in reversed(payload):
            if isinstance(item, dict):
                return item
    raise ValueError("listing JSON must be one object or a list of objects")


def record_from_payload(payload: dict[str, Any]) -> NewListingRecord:
    return NewListingRecord(
        saved_at=str(payload.get("saved_at") or payload.get("scraped_at") or ""),
        fingerprint=str(payload.get("fingerprint") or payload.get("external_id") or payload.get("url") or ""),
        title=payload.get("title"),
        price=to_int(payload.get("price")),
        address=payload.get("address"),
        description=payload.get("original_description") or payload.get("description"),
        url=payload.get("url"),
        raw_card_texts=list_of_strings(payload.get("raw_card_texts")),
        raw_detail_texts=list_of_strings(payload.get("raw_detail_texts")),
        delivery_text=payload.get("delivery_text"),
        delivery_price_rub=to_int(payload.get("delivery_price_rub")),
        delivery_status=payload.get("delivery_status"),
        seller_city=payload.get("seller_city"),
        item_kind=payload.get("item_kind") or payload.get("type"),
    )


def to_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def list_of_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None]


if __name__ == "__main__":
    raise SystemExit(main())
