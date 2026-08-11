"""Shared parsers for local market JSON imports."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any


def parse_price(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    normalized = re.sub(r"[^\d,.\-]", "", text).replace(",", ".")
    if not normalized:
        return None
    try:
        return float(normalized)
    except ValueError:
        return None


def parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return normalize_to_utc_naive(value)
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, timezone.utc).replace(tzinfo=None)
        except (OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return normalize_to_utc_naive(datetime.fromisoformat(text))
    except ValueError:
        return None


def normalize_to_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.replace(tzinfo=None)


def extract_seller_user_key(seller: Any) -> str | None:
    if not isinstance(seller, dict):
        return None
    for key in ("userId", "user_id", "id", "profileId", "profile_id"):
        value = seller.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    for key in ("url", "profileUrl", "profile_url"):
        value = seller.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def extract_platform(parameters: Any) -> str | None:
    if not isinstance(parameters, list):
        return None
    for parameter in parameters:
        if not isinstance(parameter, dict):
            continue
        label = " ".join(
            str(parameter.get(key) or "")
            for key in ("name", "title", "label", "slug", "code")
        ).lower()
        if "platform" not in label and "playstation" not in label and "ps" not in label and "платформ" not in label:
            continue
        value = parameter.get("value") or parameter.get("text") or parameter.get("displayValue")
        if isinstance(value, dict):
            value = value.get("name") or value.get("title") or value.get("value")
        if isinstance(value, list):
            value = ", ".join(str(item.get("name") if isinstance(item, dict) else item) for item in value)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None
