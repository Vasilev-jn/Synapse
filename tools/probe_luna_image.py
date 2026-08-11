from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.env import get_env  # noqa: E402
from app.llm_analyzer import POLZA_URL, DEFAULT_HEAVY_MODEL, openrouter_completion_body  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Manual one-image Luna probe. Not used by the monitor pipeline.")
    parser.add_argument("--image-url", required=True, help="Public image URL to inspect once.")
    parser.add_argument("--model", default=DEFAULT_HEAVY_MODEL)
    parser.add_argument("--fallback-model", default="")
    parser.add_argument("--output", type=Path, default=ROOT / "reports" / "luna_image_probe_latest.json")
    args = parser.parse_args()

    api_key = get_env("POLZA_API_KEY")
    if not api_key:
        print("[IMAGE-PROBE] POLZA_API_KEY is not set", file=sys.stderr)
        return 2

    result = run_probe(args.model, args.image_url, api_key)
    if not result["ok"] and args.fallback_model:
        fallback = run_probe(args.fallback_model, args.image_url, api_key)
        result = {"primary": result, "fallback": fallback}
    report = {
        "image_url": args.image_url,
        "result": result,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[IMAGE-PROBE] Saved: {args.output}")
    return 0


def run_probe(model: str, image_url: str, api_key: str) -> dict[str, object]:
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You inspect one Avito listing photo for resale inventory. "
                    "Return only JSON with keys: visible_items_count, visible_item_kinds, confidence, notes. "
                    "Do not invent exact titles if unreadable."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What sellable PS4/PS5 items are visible? Count broad item types only if exact names are unreadable."},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            },
        ],
        "temperature": 0,
        "max_tokens": 700,
        "response_format": {"type": "json_object"},
    }
    started = time.monotonic()
    body = openrouter_completion_body(payload, api_key=api_key, model=model, api_url=POLZA_URL)
    elapsed = round(time.monotonic() - started, 3)
    if isinstance(body, dict):
        return {"ok": False, "requested_model": model, "elapsed_seconds": elapsed, "error": body}
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return {"ok": False, "requested_model": model, "elapsed_seconds": elapsed, "raw": body[:1000]}
    return {
        "ok": True,
        "requested_model": model,
        "response_model": data.get("model"),
        "elapsed_seconds": elapsed,
        "content": (((data.get("choices") or [{}])[0].get("message") or {}).get("content")),
    }


if __name__ == "__main__":
    raise SystemExit(main())
