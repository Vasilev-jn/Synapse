from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.llm_analyzer import llm_api_key, llm_api_url, llm_provider, openrouter_completion_body  # noqa: E402
from app.pipeline import load_automation_settings  # noqa: E402


PRICE_MIN = 100
PRICE_MAX = 100_000


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract Avito price-list lines and optionally normalize them by LLM chunks.")
    parser.add_argument("--listing-json", type=Path, required=True)
    parser.add_argument("--index", type=int, default=None)
    parser.add_argument("--output", type=Path, default=ROOT / "reports" / "price_line_chunk_probe.json")
    parser.add_argument("--llm-model", default="")
    parser.add_argument("--chunk-size", type=int, default=30)
    parser.add_argument("--max-chunks", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    payload = select_payload(load_json(args.listing_json), index=args.index)
    text = listing_text(payload)
    price_lines = extract_price_lines(text)
    report: dict[str, Any] = {
        "source": str(args.listing_json),
        "index": args.index,
        "title": payload.get("title"),
        "url": payload.get("url"),
        "listing_price": payload.get("price"),
        "description_chars": len(str(payload.get("description") or "")),
        "price_lines_count": len(price_lines),
        "price_lines_preview": price_lines[:50],
        "llm_chunks": [],
    }

    if args.llm_model and args.max_chunks > 0:
        report["llm_chunks"] = run_llm_chunks(
            price_lines,
            model=args.llm_model,
            chunk_size=max(1, args.chunk_size),
            max_chunks=max(1, args.max_chunks),
            timeout=max(30, args.timeout),
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[PRICE-LINES] count={len(price_lines)} saved={args.output}")
    if report["llm_chunks"]:
        for chunk in report["llm_chunks"]:
            print(
                f"[LLM-CHUNK] chunk={chunk['chunk_index']} ok={chunk['ok']} "
                f"items={chunk.get('items_count')} elapsed={chunk.get('elapsed_seconds')}"
            )
    return 0


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def select_payload(payload: Any, *, index: int | None) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        if index is None:
            for item in reversed(payload):
                if isinstance(item, dict):
                    return item
        else:
            item = payload[index]
            if isinstance(item, dict):
                return item
    raise ValueError("listing JSON must be one object or a list of objects")


def listing_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("title", "description"):
        value = payload.get(key)
        if value:
            parts.append(str(value))
    for key in ("raw_card_texts", "raw_detail_texts"):
        value = payload.get(key)
        if isinstance(value, list):
            parts.extend(str(item) for item in value if item is not None)
    return "\n".join(parts)


def extract_price_lines(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    previous_without_price = ""
    seen: set[tuple[str, int]] = set()
    for line_no, raw_line in enumerate(re.split(r"[\r\n]+", text), start=1):
        line = clean_line(raw_line)
        if not line:
            continue
        if unavailable_line(line):
            previous_without_price = ""
            continue
        parsed = parse_price_line(line)
        if parsed is None and previous_without_price:
            parsed = parse_price_line(f"{previous_without_price} {line}")
        if parsed is None:
            if likely_name_fragment(line):
                previous_without_price = line
            continue
        previous_without_price = ""
        name, price = parsed
        key = (normalize_key(name), price)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "line_no": line_no,
                "name": name,
                "price_rub": price,
                "source_line": line,
            }
        )
    return rows


def parse_price_line(line: str) -> tuple[str, int] | None:
    patterns = (
        r"^(?P<name>.+?)[\s_\-–—:]+(?P<price>\d[\d\s]{2,5})\s*(?:₽|руб\.?|р\.?|rub)?\.?$",
        r"^(?P<name>.+?)\s+(?P<price>\d{3,5})\s*(?:₽|руб\.?|р\.?|rub)?\.?$",
    )
    for pattern in patterns:
        match = re.match(pattern, line, flags=re.IGNORECASE)
        if not match:
            continue
        name = clean_name(match.group("name"))
        price = parse_int(match.group("price"))
        if not name or price is None or price < PRICE_MIN or price > PRICE_MAX:
            continue
        if looks_like_non_product_name(name):
            continue
        return name, price
    return None


def clean_line(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip(" .")


def clean_name(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" -–—_:.,")


def parse_int(value: str) -> int | None:
    try:
        return int(re.sub(r"\D+", "", value))
    except ValueError:
        return None


def unavailable_line(line: str) -> bool:
    lowered = line.lower().replace("ё", "е")
    return any(marker in lowered for marker in ("нету", "нет в наличии", "продано"))


def likely_name_fragment(line: str) -> bool:
    if len(line) < 3 or len(line) > 80:
        return False
    if re.search(r"\d{3,5}", line):
        return False
    lowered = line.lower()
    return not any(marker in lowered for marker in ("цена", "доставка", "самовывоз", "авито"))


def looks_like_non_product_name(name: str) -> bool:
    lowered = name.lower().replace("ё", "е")
    bad_markers = ("цена", "доставка", "самовывоз", "отправ", "торг", "скидк")
    return any(marker in lowered for marker in bad_markers)


def normalize_key(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold().replace("ё", "е")).strip()


def run_llm_chunks(
    price_lines: list[dict[str, Any]],
    *,
    model: str,
    chunk_size: int,
    max_chunks: int,
    timeout: int,
) -> list[dict[str, Any]]:
    settings = load_automation_settings()
    provider = llm_provider(settings)
    api_key = llm_api_key(provider)
    if not api_key:
        return [{"ok": False, "error": f"missing API key for provider={provider}"}]
    api_url = llm_api_url(provider)
    reports: list[dict[str, Any]] = []
    for chunk_index, start in enumerate(range(0, len(price_lines), chunk_size), start=1):
        if chunk_index > max_chunks:
            break
        chunk = price_lines[start : start + chunk_size]
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Normalize Avito price-list rows for PS4/PS5 resale. "
                        "Return only JSON {\"items\": [...]}. Return one item per input row. "
                        "Do not drop rows. Do not invent prices. Keep original_name if unsure."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "task": "Normalize these already extracted product-price rows.",
                            "schema": {
                                "items": [
                                    {
                                        "line_no": 1,
                                        "original_name": "raw product name",
                                        "normalized_name": "clean readable title",
                                        "price_rub": 1000,
                                        "platform": "PS4 | PS5 | unknown",
                                        "item_type": "game | accessory | console | unknown",
                                        "confidence": "high | medium | low",
                                    }
                                ]
                            },
                            "rows": chunk,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "temperature": 0,
            "max_tokens": 6000,
            "response_format": {"type": "json_object"},
        }
        started = time.monotonic()
        body = openrouter_completion_body(
            payload,
            api_key=api_key,
            model=model,
            api_url=api_url,
            request_timeout_seconds=timeout,
        )
        elapsed = round(time.monotonic() - started, 3)
        reports.append(parse_llm_chunk_result(body, chunk_index=chunk_index, model=model, elapsed=elapsed, timeout=timeout))
    return reports


def parse_llm_chunk_result(body: str | dict[str, object], *, chunk_index: int, model: str, elapsed: float, timeout: int) -> dict[str, Any]:
    if isinstance(body, dict):
        return {"chunk_index": chunk_index, "ok": False, "model": model, "elapsed_seconds": elapsed, "timeout": timeout, "error": body}
    try:
        data = json.loads(body)
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        items = parsed.get("items") if isinstance(parsed, dict) else None
    except Exception as error:
        return {
            "chunk_index": chunk_index,
            "ok": False,
            "model": model,
            "elapsed_seconds": elapsed,
            "timeout": timeout,
            "error": str(error),
            "raw": body[:1000],
        }
    return {
        "chunk_index": chunk_index,
        "ok": isinstance(items, list),
        "model": model,
        "response_model": data.get("model"),
        "elapsed_seconds": elapsed,
        "timeout": timeout,
        "items_count": len(items) if isinstance(items, list) else 0,
        "items_preview": items[:10] if isinstance(items, list) else [],
    }


if __name__ == "__main__":
    raise SystemExit(main())
