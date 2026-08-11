"""Evaluate Avito listings from ADB bot extracted item data."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PriceObservation
from app.price_observations import (
    OBS_AVITO_MARKET,
    OBS_FALLBACK,
    OBS_OWN_AVITO_ACTIVE,
    OBS_OWN_SALE,
    OBS_PARTNER_SALE,
    fallback_confidence,
    fallback_price,
    price_observation_is_usable_for_auto_price,
    safe_float,
)
from app.schemas import parse_price
from app.suggestion_sources import normalize_lookup_text, profit_knowledge


NET_AFTER_SALE_RATE = 0.87
SOURCE_WEIGHTS = {
    OBS_OWN_SALE: 1.0,
    OBS_PARTNER_SALE: 0.85,
    OBS_OWN_AVITO_ACTIVE: 0.7,
    OBS_AVITO_MARKET: 0.55,
    OBS_FALLBACK: 0.3,
}
PRICE_OBSERVATIONS_CACHE_KEY = "usable_price_observations"
PRICE_ESTIMATES_CACHE_KEY = "sale_price_estimates"
PRIMARY_MARKET_DAYS = 30
EXTENDED_MARKET_DAYS = 90
PRICE_TRIM_PERCENT = 0.03
PRICE_TRIM_MIN_COUNT = 20
GENERIC_MATCH_TOKENS = {
    "ps",
    "ps4",
    "ps5",
    "playstation",
    "sony",
    "game",
    "games",
    "disc",
    "disk",
    "for",
    "on",
    "new",
    "edition",
    "collection",
    "the",
    "of",
    "and",
    "игра",
    "игры",
    "диск",
    "диски",
    "для",
    "на",
    "сони",
    "плейстейшен",
    "коллекция",
    "часть",
    "лот",
    "лотом",
    "комплект",
    "набор",
    "новый",
    "новая",
}


@dataclass
class PriceCandidate:
    price: float
    weight: float
    canonical_name: str
    item_type: str
    source: str
    confidence: float
    reason: str
    price_source: str
    matched_by: str
    match_score: float


@dataclass
class CachedPriceObservation:
    price: float
    source: str
    source_weight: float
    confidence: float
    recency: float
    market_quality: float
    observed_at: datetime | None
    platform_family: str | None
    catalog_entry_id: int | None
    canonical_name: str
    item_type: str | None
    reason: str
    normalized_values: tuple[str, ...]


def evaluate_avito_listing_payload(
    session: Session,
    *,
    listing: dict[str, Any],
    extracted_items: list[dict[str, Any]],
) -> dict[str, Any]:
    raw_json = listing_raw_json(listing)
    listing_price = payload_price(listing, "price", "listing_price")
    delivery_price = payload_delivery_price(listing)
    buy_total = (listing_price or 0.0) + (delivery_price or 0.0)
    risks: list[str] = []
    risks.extend(listing_content_risks(listing, raw_json))
    if listing_price is None:
        risks.append("listing_price_missing")
    if delivery_price is None:
        risks.append("delivery_unknown")
        delivery_price = 0.0

    source_items = extracted_items_for_evaluation(listing, extracted_items)
    if not source_items:
        risks.append("no_extracted_items")

    normalized_items = normalize_extracted_items(source_items, listing=listing)
    account_bonus = console_account_subscription_bonus(
        listing=listing,
        raw_json=raw_json,
        source_items=source_items,
        normalized_items=normalized_items,
    )
    has_lot_costs = apply_lot_cost_observations(normalized_items, raw_json)
    has_explicit_buy_prices = any(item["has_explicit_buy_price"] for item in normalized_items)
    weighted_bundle_cost = (
        not has_lot_costs
        and not has_explicit_buy_prices
        and len(normalized_items) > 1
        and buy_total > 0
    )
    if weighted_bundle_cost:
        risks.append("lot_price_weighted_by_market_value")
    else:
        distribute_buy_prices(normalized_items, listing_price or 0.0, delivery_price or 0.0, has_lot_costs=has_lot_costs)

    response_items: list[dict[str, Any]] = []
    expected_sell_total = 0.0
    expected_net_total = 0.0
    matched_count = 0
    matched_sources: list[str] = []

    for item in normalized_items:
        quantity = item["quantity"]
        buy_price_used = item["buy_price_used"]
        if "console_storage_missing" in risks and item_is_console(item):
            response_items.append(
                {
                    "input_name": item["name"],
                    "input_item_type": item["item_type"] or "unknown",
                    "name": item["name"],
                    "canonical_name": item.get("canonical_name"),
                    "item_type": item["item_type"] or "unknown",
                    "catalog_item_id": item.get("catalog_item_id"),
                    "matched_catalog_name": None,
                    "matched_catalog_type": None,
                    "matched_by": None,
                    "match_score": 0.0,
                    "quantity": quantity,
                    "buy_price": round_money(buy_price_used) if buy_price_used is not None else None,
                    "buy_price_used": round_money(buy_price_used) if buy_price_used is not None else None,
                    "expected_sell_price": None,
                    "expected_net": None,
                    "expected_profit": None,
                    "confidence": "none",
                    "source": None,
                    "price_source": None,
                    "reason": "консоль без HDD/накопителя; не считаем как полноценную рыночную консоль",
                }
            )
            continue
        result = evaluate_extracted_item(session, item)
        if result is None:
            risks.append(f"no_price_for:{item['name']}")
            if item.get("unsafe_match_reason"):
                risks.append(str(item["unsafe_match_reason"]))
            response_items.append(
                {
                    "input_name": item["name"],
                    "input_item_type": item["item_type"] or "unknown",
                    "name": item["name"],
                    "canonical_name": None,
                    "item_type": item["item_type"] or "unknown",
                    "catalog_item_id": item.get("catalog_item_id"),
                    "matched_catalog_name": None,
                    "matched_catalog_type": None,
                    "matched_by": None,
                    "match_score": 0.0,
                    "quantity": quantity,
                    "buy_price": round_money(buy_price_used) if buy_price_used is not None else None,
                    "buy_price_used": round_money(buy_price_used) if buy_price_used is not None else None,
                    "expected_sell_price": None,
                    "expected_net": None,
                    "expected_profit": None,
                    "confidence": "none",
                    "source": None,
                    "price_source": None,
                    "reason": "цена не найдена в CRM-базе; профит не учитывает этот товар",
                }
            )
            continue

        matched_count += 1
        matched_sources.append(result["source"])
        expected_sell = result["expected_sell_price"]
        expected_net = round_money(expected_sell * NET_AFTER_SALE_RATE)
        expected_profit = round_money(expected_net - buy_price_used) if buy_price_used is not None else None
        expected_sell_total += expected_sell * quantity
        expected_net_total += expected_net * quantity
        response_items.append(
            {
                "input_name": item["name"],
                "input_item_type": item["item_type"] or "unknown",
                "name": item["name"],
                "canonical_name": result["canonical_name"],
                "item_type": result["item_type"],
                "catalog_item_id": item.get("catalog_item_id"),
                "matched_catalog_name": result["matched_catalog_name"],
                "matched_catalog_type": result["matched_catalog_type"],
                "matched_by": result["matched_by"],
                "match_score": result["match_score"],
                "quantity": quantity,
                "buy_price": round_money(buy_price_used) if buy_price_used is not None else None,
                "buy_price_used": round_money(buy_price_used) if buy_price_used is not None else None,
                "expected_sell_price": expected_sell,
                "expected_net": expected_net,
                "expected_profit": expected_profit,
                "confidence": result["confidence"],
                "source": result["source"],
                "price_source": result["price_source"],
                "reason": item.get("buy_price_reason") or result_reason(result),
            }
        )

    if weighted_bundle_cost:
        allocate_bundle_buy_prices_by_market_value(response_items, buy_total, risks)

    if account_bonus:
        expected_net_total += account_bonus["amount"]
        risks.append(account_bonus["risk"])
        response_items.append(
            {
                "input_name": account_bonus["name"],
                "input_item_type": "subscription",
                "name": account_bonus["name"],
                "canonical_name": None,
                "item_type": "subscription",
                "catalog_item_id": None,
                "matched_catalog_name": None,
                "matched_catalog_type": None,
                "matched_by": "bundle_bonus",
                "match_score": 0.0,
                "quantity": 1,
                "buy_price": 0.0,
                "buy_price_used": 0.0,
                "expected_sell_price": account_bonus["amount"],
                "expected_net": account_bonus["amount"],
                "expected_profit": account_bonus["amount"],
                "confidence": "low",
                "source": "console_account_subscription_bonus",
                "price_source": "manual_rule",
                "reason": account_bonus["reason"],
            }
        )

    expected_profit_total = round_money(expected_net_total - buy_total)
    profit_percent = round(expected_profit_total / buy_total * 100, 1) if buy_total > 0 else None
    fallback_only = bool(matched_sources) and all(source == OBS_FALLBACK for source in matched_sources)
    decision = listing_decision(
        matched_count=matched_count,
        total_count=len(normalized_items),
        expected_profit=expected_profit_total,
        profit_percent=profit_percent,
        risks=risks,
        fallback_only=fallback_only,
    )
    summary = listing_summary(decision, matched_count, expected_profit_total)
    if fallback_only:
        summary = f"{summary} Источник цены: fallback / низкая уверенность."
    human_reason = (
        f"Цена объявления {money_text(listing_price)}"
        f", доставка {money_text(delivery_price)}. "
        f"По распознанным товарам ожидаемая чистая выручка {money_text(expected_net_total)}, "
        f"примерный профит {money_text(expected_profit_total)}."
    )
    if fallback_only:
        human_reason = f"{human_reason} Цена взята из fallback / низкая уверенность."
    return {
        "ok": True,
        "bot_listing_id": listing.get("bot_listing_id") or listing.get("external_id"),
        "external_id": listing.get("external_id"),
        "decision": decision,
        "summary": summary,
        "total": {
            "listing_price": round_money(listing_price),
            "delivery_price": round_money(delivery_price),
            "buy_total": round_money(buy_total),
            "expected_sell_total": round_money(expected_sell_total),
            "expected_net_total": round_money(expected_net_total),
            "expected_profit": expected_profit_total,
            "profit_percent": profit_percent,
        },
        "items": response_items,
        "risks": risks,
        "sources_used": sorted(set(matched_sources)),
        "fallback_only": fallback_only,
        "human_reason": human_reason,
    }


def evaluate_extracted_item(session: Session, item: dict[str, Any]) -> dict[str, Any] | None:
    return estimate_sale_price_for_name(
        session,
        name=str(item.get("canonical_name") or item["name"]),
        platform=item.get("platform"),
        item_type=item.get("item_type"),
        catalog_item_id=item.get("catalog_item_id"),
        catalog_entry_id=item.get("catalog_entry_id"),
        display_name=item["name"],
    )


def result_reason(result: dict[str, Any]) -> str:
    if result.get("source") == OBS_FALLBACK:
        return f"fallback / низкая уверенность: {result['reason']}"
    return str(result["reason"])


def estimate_sale_price_for_name(
    session: Session,
    *,
    name: str,
    platform: Any = None,
    item_type: Any = None,
    catalog_item_id: Any = None,
    catalog_entry_id: Any = None,
    display_name: Any = None,
) -> dict[str, Any] | None:
    cache_key = estimate_cache_key(name, platform, item_type, catalog_item_id, catalog_entry_id)
    cached_estimates = session.info.setdefault(PRICE_ESTIMATES_CACHE_KEY, {})
    if cache_key in cached_estimates:
        cached = cached_estimates[cache_key]
        return dict(cached) if cached is not None else None

    candidates = price_candidates_for_item(
        session,
        {
            "name": name,
            "display_name": display_name,
            "platform": platform,
            "item_type": item_type,
            "catalog_item_id": catalog_item_id,
            "catalog_entry_id": catalog_entry_id,
        },
    )
    if not candidates:
        cached_estimates[cache_key] = None
        return None
    manual_baseline = manual_baseline_candidate(candidates)
    if manual_baseline is not None:
        estimate = price_estimate_from_candidate(
            manual_baseline,
            candidates=candidates,
            price_source="manual_market_baseline",
        )
        cached_estimates[cache_key] = estimate
        return dict(estimate)
    median_candidates = trim_price_candidates(candidates)
    expected_sell_price = round_price(weighted_median(median_candidates))
    best = max(median_candidates, key=lambda candidate: candidate.weight)
    total_weight = sum(candidate.weight for candidate in median_candidates)
    estimate = {
        "expected_sell_price": expected_sell_price,
        "canonical_name": best.canonical_name,
        "item_type": best.item_type,
        "matched_catalog_name": best.canonical_name,
        "matched_catalog_type": best.item_type,
        "matched_by": best.matched_by,
        "match_score": best.match_score,
        "confidence": confidence_label(total_weight, max(candidate.confidence for candidate in median_candidates), len(median_candidates)),
        "confidence_score": max(candidate.confidence for candidate in median_candidates),
        "source": best.source,
        "reason": best.reason,
        "price_source": best.price_source,
        "candidates_count": len(median_candidates),
        "raw_candidates_count": len(candidates),
        "trimmed_low_count": trim_count_for_candidates(candidates),
        "trimmed_high_count": trim_count_for_candidates(candidates),
        "total_weight": total_weight,
    }
    cached_estimates[cache_key] = estimate
    return dict(estimate)


def manual_baseline_candidate(candidates: list[PriceCandidate]) -> PriceCandidate | None:
    manual = [candidate for candidate in candidates if candidate.reason == "ручной baseline Кирилла"]
    if not manual:
        return None
    return max(manual, key=lambda candidate: (candidate.match_score, candidate.confidence, candidate.weight))


def price_estimate_from_candidate(
    candidate: PriceCandidate,
    *,
    candidates: list[PriceCandidate],
    price_source: str,
) -> dict[str, Any]:
    return {
        "expected_sell_price": round_money(candidate.price),
        "canonical_name": candidate.canonical_name,
        "item_type": candidate.item_type,
        "matched_catalog_name": candidate.canonical_name,
        "matched_catalog_type": candidate.item_type,
        "matched_by": candidate.matched_by,
        "match_score": candidate.match_score,
        "confidence": confidence_label(candidate.weight, candidate.confidence, 1),
        "confidence_score": candidate.confidence,
        "source": candidate.source,
        "reason": candidate.reason,
        "price_source": price_source,
        "candidates_count": 1,
        "raw_candidates_count": len(candidates),
        "trimmed_low_count": 0,
        "trimmed_high_count": 0,
        "total_weight": candidate.weight,
    }


def price_candidates_for_item(session: Session, item: dict[str, Any]) -> list[PriceCandidate]:
    name = item["name"]
    platform = item.get("platform")
    item_type = item.get("item_type")
    requested_types = price_item_types(name, item_type)
    if not requested_types or generic_unknown_item_name(item):
        return []
    needles = price_lookup_needles(item)
    if not needles:
        return []
    requested_family = platform_family(platform)
    strict_identity = has_strict_identity(item)
    for period_days in (PRIMARY_MARKET_DAYS, EXTENDED_MARKET_DAYS):
        candidates: list[PriceCandidate] = []
        for observation in cached_price_observations(session):
            if not observation_within_days(observation, period_days):
                continue
            if requested_family and observation.platform_family and requested_family != observation.platform_family:
                continue
            if not observation_item_type_matches(requested_types, observation):
                continue
            if strict_identity and not strict_observation_identity_matches(item, observation):
                continue
            match = cached_observation_match(needles, item, observation)
            if match["score"] <= 0:
                continue
            confidence = observation.confidence
            weight = observation.source_weight * confidence * observation.recency * observation.market_quality * match["score"]
            if weight <= 0:
                continue
            candidates.append(
                PriceCandidate(
                    price=observation.price,
                    weight=weight,
                    canonical_name=observation.canonical_name,
                    item_type=observation.item_type or next(iter(requested_types)),
                    source=observation.source,
                    confidence=confidence,
                    reason=observation.reason,
                    price_source=f"market_observations_{period_days}d",
                    matched_by=match["matched_by"],
                    match_score=match["score"],
                )
            )
        if candidates:
            return candidates

    fallback = fallback_candidate_for_item(item, requested_types=requested_types)
    return [fallback] if fallback is not None else []


def cached_price_observations(session: Session) -> list[CachedPriceObservation]:
    cached = session.info.get(PRICE_OBSERVATIONS_CACHE_KEY)
    if cached is None:
        cached = [
            cached_row
            for row in session.scalars(
                select(PriceObservation).where(
                    PriceObservation.price.is_not(None),
                    PriceObservation.price > 0,
                    PriceObservation.usable_for_auto_price.is_(True),
                    PriceObservation.observation_type != OBS_FALLBACK,
                )
            )
            if price_observation_is_usable_for_auto_price(row)
            for cached_row in [cached_price_observation(row)]
        ]
        session.info[PRICE_OBSERVATIONS_CACHE_KEY] = cached
    return cached


def cached_price_observation(observation: PriceObservation) -> CachedPriceObservation:
    raw_json = observation.raw_json if isinstance(observation.raw_json, dict) else {}
    normalized_values = normalized_observation_values(observation, raw_json)
    confidence = float(observation.confidence or 0.0)
    return CachedPriceObservation(
        price=float(observation.price),
        source=observation.observation_type,
        source_weight=source_weight_for_observation(observation, raw_json),
        confidence=confidence,
        recency=recency_weight(observation.observed_at),
        market_quality=market_quality_weight(observation, raw_json),
        observed_at=observation.observed_at,
        platform_family=platform_family(observation.platform_or_model),
        catalog_entry_id=observation.catalog_entry_id,
        canonical_name=str(raw_json.get("matched_name") or raw_json.get("canonical_name") or observation.item_title or ""),
        item_type=cached_observation_item_type(observation, raw_json),
        reason=observation_reason(observation),
        normalized_values=normalized_values,
    )


def source_weight_for_observation(observation: PriceObservation, raw_json: dict[str, Any]) -> float:
    if raw_json.get("source_kind") == "manual_market_baseline":
        return 10.0
    return SOURCE_WEIGHTS.get(observation.observation_type, 0.4)


def normalized_observation_values(observation: PriceObservation, raw_json: dict[str, Any]) -> tuple[str, ...]:
    values = [observation.item_title or ""]
    values.extend(
        str(raw_json.get(key) or "")
        for key in ("matched_name", "canonical_name", "title", "name", "catalog_item_id", "catalog_entry_id")
    )
    if observation.catalog_entry_id is not None:
        values.append(str(observation.catalog_entry_id))
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = normalize_lookup_text(value)
        if text and text not in seen:
            seen.add(text)
            normalized.append(text)
    return tuple(normalized)


def cached_observation_match(needles: list[str], item: dict[str, Any], observation: CachedPriceObservation) -> dict[str, Any]:
    if not needles:
        return {"score": 0.0, "matched_by": None}
    if observation_id_matches(item, observation):
        return {"score": 1.0, "matched_by": "catalog_id"}

    canonical = normalize_lookup_text(str(item.get("canonical_name") or "").replace("_", " "))
    if canonical and canonical in observation.normalized_values:
        return {"score": 1.0, "matched_by": "canonical_name"}

    best_score = 0.0
    for needle in needles:
        for value in observation.normalized_values:
            best_score = max(best_score, text_match_quality(needle, value))
    if best_score <= 0:
        return {"score": 0.0, "matched_by": None}
    return {"score": best_score, "matched_by": "alias" if best_score >= 1.0 else "fuzzy"}


def estimate_cache_key(name: str, platform: Any, item_type: Any, catalog_item_id: Any = None, catalog_entry_id: Any = None) -> tuple[str, str, str, str, str]:
    return (
        normalize_lookup_text(str(name or "")),
        platform_family(platform) or normalize_lookup_text(str(platform or "")),
        normalize_lookup_text(str(item_type or "")),
        normalize_lookup_text(str(catalog_item_id or "")),
        normalize_lookup_text(str(catalog_entry_id or "")),
    )


def price_lookup_needles(item: dict[str, Any]) -> list[str]:
    values = [
        item.get("catalog_entry_id"),
        item.get("catalog_item_id"),
        item.get("canonical_name"),
        item.get("name"),
        item.get("display_name"),
    ]
    needles: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = normalize_lookup_text(str(value or "").replace("_", " "))
        if text and text not in seen:
            seen.add(text)
            needles.append(text)
    return needles


def has_strict_identity(item: dict[str, Any]) -> bool:
    return bool(item.get("catalog_item_id") or item.get("catalog_entry_id"))


def strict_observation_identity_matches(item: dict[str, Any], observation: CachedPriceObservation) -> bool:
    return observation_id_matches(item, observation)


def observation_id_matches(item: dict[str, Any], observation: CachedPriceObservation) -> bool:
    requested_ids = {
        normalize_lookup_text(str(value or "").replace("_", " "))
        for value in (item.get("catalog_item_id"), item.get("catalog_entry_id"))
        if value
    }
    observation_ids = {value for value in observation.normalized_values if value in requested_ids}
    if observation.catalog_entry_id is not None:
        observation_ids.add(normalize_lookup_text(str(observation.catalog_entry_id)))
    if requested_ids and requested_ids & observation_ids:
        return True
    return False


def observation_item_type_matches(requested_types: set[str], observation: CachedPriceObservation) -> bool:
    if not requested_types:
        return False
    observed_type = price_item_type_key(observation.item_type, observation.canonical_name)
    if observed_type:
        return observed_type in requested_types
    return False


def price_item_types(name: Any, item_type: Any) -> set[str]:
    key = price_item_type_key(item_type, name)
    if key:
        return {key}
    return set()


def price_item_type_key(value: Any, title: Any = None) -> str | None:
    text = normalize_lookup_text(str(value or ""))
    if any(token in text for token in ("game", "disc", "disk", "игра", "диск")):
        return "game"
    if any(token in text for token in ("console", "console bundle", "пристав", "консоль")):
        return "console"
    if "playstation" in text and any(token in text for token in ("slim", "fat", "pro", "500", "1tb", "1тб")):
        return "console"
    if any(token in text for token in ("controller", "gamepad", "dualshock", "dualsense", "джоист", "джостик", "геймпад")):
        return "controller"
    if any(token in text for token in ("accessory", "аксессуар")):
        return "accessory"
    if observation_title_is_generic_platform(str(title or "")):
        return "console"
    return None


def cached_observation_item_type(observation: PriceObservation, raw_json: dict[str, Any]) -> str | None:
    for value in (
        raw_json.get("item_type"),
        raw_json.get("type"),
        raw_json.get("category"),
        raw_json.get("source_kind"),
        observation.item_title,
    ):
        key = price_item_type_key(value, observation.item_title)
        if key:
            return key
    return None


def generic_unknown_item_name(item: dict[str, Any]) -> bool:
    text = normalize_lookup_text(
        " ".join(
            str(value or "")
            for value in (
                item.get("name"),
                item.get("display_name"),
                item.get("canonical_name"),
            )
        )
    )
    if not text:
        return True
    generic_terms = (
        "unknown",
        "undefined",
        "unspecified",
        "not specified",
        "photo",
        "list",
        "games",
        "controllers",
        "unknown ps4",
        "unknown ps5",
        "неизвест",
        "не указ",
        "неизв",
        "список",
        "фото",
        "игры не указ",
    )
    return any(term in text for term in generic_terms)


def observation_has_generic_platform_title(observation: CachedPriceObservation) -> bool:
    return any(observation_title_is_generic_platform(value) for value in observation.normalized_values)


def observation_title_is_generic_platform(value: str) -> bool:
    text = normalize_lookup_text(str(value or ""))
    if not platform_family(text):
        return False
    generic_tokens = {
        "sony",
        "playstation",
        "ps4",
        "ps5",
        "4",
        "5",
        "game",
        "games",
        "disc",
        "disk",
        "for",
        "on",
        "игра",
        "игры",
        "диск",
        "диски",
        "для",
        "на",
        "сони",
        "плейстейшен",
        "приставка",
        "консоль",
    }
    tokens = set(text.split())
    return bool(tokens) and not (tokens - generic_tokens)


def fallback_candidate_for_item(item: dict[str, Any], *, requested_types: set[str]) -> PriceCandidate | None:
    knowledge = profit_knowledge()
    if not knowledge.items_by_id or not requested_types:
        return None

    requested_ids = {
        normalize_lookup_text(str(value or "").replace("_", " "))
        for value in (item.get("catalog_item_id"), item.get("catalog_entry_id"))
        if value
    }
    if requested_ids:
        for candidate in knowledge.items_by_id.values():
            candidate_id = normalize_lookup_text(str(candidate.id or "").replace("_", " "))
            if candidate.item_type in requested_types and candidate_id in requested_ids:
                return fallback_price_candidate(candidate, matched_by="fallback", match_score=1.0)
        return None

    canonical = normalize_lookup_text(str(item.get("canonical_name") or "").replace("_", " "))
    name = normalize_lookup_text(str(item.get("name") or "").replace("_", " "))
    if not canonical and (not name or generic_unknown_item_name(item)):
        return None

    term = canonical or name
    best_score = 0
    best_item = None
    for alias, item_id, weight in knowledge.aliases:
        candidate = knowledge.items_by_id.get(item_id)
        if candidate is None or candidate.item_type not in requested_types or not alias:
            continue
        score = fallback_alias_match_score(term, alias, weight, exact_only=bool(canonical))
        if score > best_score:
            best_score = score
            best_item = candidate
    if best_item is None or best_score <= 0:
        return None
    return fallback_price_candidate(best_item, matched_by="fallback", match_score=min(1.0, best_score / 100_000))


def fallback_price_candidate(candidate: Any, *, matched_by: str, match_score: float) -> PriceCandidate | None:
    price, basis = fallback_price(candidate)
    if price is None or price <= 0:
        return None
    confidence = fallback_confidence(candidate.price_confidence)
    return PriceCandidate(
        price=float(price),
        weight=SOURCE_WEIGHTS[OBS_FALLBACK] * confidence,
        canonical_name=candidate.canonical_name,
        item_type=candidate.item_type,
        source=OBS_FALLBACK,
        confidence=confidence,
        price_source="fallback",
        reason=f"fallback crm_profit_knowledge.zip, field {basis}",
        matched_by=matched_by,
        match_score=round(match_score, 3),
    )


def fallback_alias_match_score(name: str, alias: str, weight: int, *, exact_only: bool) -> int:
    if name == alias:
        return 100_000 + weight
    if exact_only:
        return 0
    if len(alias) >= 5 and f" {alias} " in f" {name} ":
        return 80_000 + weight + len(alias)
    if len(alias) >= 6 and (alias in name or name in alias):
        return 60_000 + weight + len(alias)
    if text_match_quality(name, alias) >= 0.9:
        return 40_000 + weight + len(alias)
    return 0


def listing_raw_json(listing: dict[str, Any]) -> dict[str, Any]:
    raw_json = listing.get("raw_json")
    return raw_json if isinstance(raw_json, dict) else {}


def extracted_items_for_evaluation(listing: dict[str, Any], extracted_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if extracted_items:
        return extracted_items
    raw_json = listing_raw_json(listing)
    for key in ("llm_extracted_items", "llm_lot_cost_observations", "llm_market_price_observations"):
        rows = raw_json.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def apply_lot_cost_observations(items: list[dict[str, Any]], raw_json: dict[str, Any]) -> bool:
    rows = raw_json.get("llm_lot_cost_observations")
    if not isinstance(rows, list) or not rows:
        return False
    matched = False
    for item in items:
        item_keys = lookup_keys_for_payload(item)
        for row in rows:
            if not isinstance(row, dict):
                continue
            cost = first_price(row, "lot_unit_buy_price_rub", "lot_unit_buy_price", "buy_price_rub", "cost")
            if cost is None:
                continue
            row_keys = lookup_keys_for_payload(row)
            if item_keys & row_keys:
                item["buy_price_used"] = cost
                item["has_explicit_buy_price"] = True
                item["buy_price_reason"] = f"Себестоимость из лота {round_money(cost)} ₽"
                if not item.get("canonical_name") and row.get("canonical_name"):
                    item["canonical_name"] = str(row.get("canonical_name"))
                if not item.get("catalog_item_id") and row.get("catalog_item_id"):
                    item["catalog_item_id"] = str(row.get("catalog_item_id"))
                matched = True
                break
    return matched


def lookup_keys_for_payload(payload: dict[str, Any]) -> set[str]:
    keys = set()
    for key in ("catalog_item_id", "catalog_entry_id", "canonical_name", "name", "title"):
        value = str(payload.get(key) or "").strip()
        if value:
            keys.add(normalize_lookup_text(value.replace("_", " ")))
    return {key for key in keys if key}


def listing_content_risks(listing: dict[str, Any], raw_json: dict[str, Any]) -> list[str]:
    link_payload = raw_json.get("link_monitor_payload") if isinstance(raw_json.get("link_monitor_payload"), dict) else {}
    text = normalize_lookup_text(
        " ".join(
            str(value or "")
            for value in (
                listing.get("title"),
                listing.get("description"),
                link_payload.get("description"),
                link_payload.get("original_description"),
                listing.get("format"),
                listing.get("type"),
                raw_json.get("description"),
                raw_json.get("format"),
                raw_json.get("type"),
            )
        )
    )
    risks: list[str] = []
    if any(token in text for token in ("digital", "цифров", "аккаунт")):
        risks.append("digital_product")
    if any(token in text for token in ("rent", "rental", "аренд", "прокат")):
        risks.append("rental")
    if any(token in text for token in ("subscription", "подпис")):
        risks.append("subscription")
    if has_missing_console_storage_text(text):
        risks.append("console_storage_missing")
    return risks


def has_missing_console_storage_text(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            "жесткий диск отсутствует",
            "жесткого диска нет",
            "без жесткого диска",
            "без жесткий диск",
            "hdd отсутствует",
            "без hdd",
            "нет hdd",
            "без накопителя",
            "накопитель отсутствует",
        )
    )


def item_is_console(item: dict[str, Any]) -> bool:
    item_type = str(item.get("item_type") or "").strip().lower()
    if item_type in {"console", "game_console"}:
        return True
    text = normalize_lookup_text(" ".join(str(item.get(key) or "") for key in ("name", "canonical_name", "catalog_item_id")))
    return bool(re.search(r"\b(ps4|ps5|playstation\s*[45])\b", text))


def listing_guard_text(listing: dict[str, Any]) -> str:
    raw_json = listing_raw_json(listing)
    parts: list[str] = [
        str(listing.get("title") or ""),
        str(listing.get("description") or ""),
    ]
    for key in ("raw_card_texts", "raw_detail_texts"):
        rows = raw_json.get(key)
        if isinstance(rows, list):
            parts.extend(str(row or "") for row in rows)
    return "\n".join(part for part in parts if part)


def first_listing_text_line(listing_text: str) -> str:
    for line in str(listing_text or "").splitlines():
        clean = line.strip()
        if clean:
            return clean
    return ""


def distinctive_tokens(value: Any) -> set[str]:
    return {
        token
        for token in normalize_lookup_text(str(value or "")).split()
        if len(token) >= 3 and token not in GENERIC_MATCH_TOKENS and not token.isdigit()
    }


def has_distinctive_overlap(left: Any, right: Any) -> bool:
    left_tokens = distinctive_tokens(left)
    right_tokens = distinctive_tokens(right)
    return bool(left_tokens and right_tokens and left_tokens & right_tokens)


def listing_has_strong_console_evidence(listing_text: str) -> bool:
    text = normalize_lookup_text(listing_text)
    return bool(
        re.search(r"\b(ps4|ps5|playstation 4|playstation 5)\b", text)
        and any(
            marker in text
            for marker in (
                "slim",
                "fat",
                "pro",
                "digital",
                "500",
                "825",
                "1tb",
                "1 тб",
                "1000",
                "cuh",
                "приставка",
                "приставку",
                "консоль",
                "консолью",
            )
        )
    )


def listing_looks_like_game_disc(listing_text: str) -> bool:
    text = normalize_lookup_text(listing_text)
    return any(marker in text for marker in ("диск", "диски", "игра", "игры", "disc", "disk", "game")) and bool(
        re.search(r"\b(ps4|ps5|playstation 4|playstation 5)\b", text)
    )


def clear_unsafe_catalog_match(item: dict[str, Any], *, listing_text: str, reason: str) -> None:
    title = first_listing_text_line(listing_text)
    if title:
        item["name"] = title
    item["canonical_name"] = None
    item["catalog_item_id"] = None
    item["catalog_entry_id"] = None
    item["unsafe_match_reason"] = reason
    if item_is_console(item) and listing_looks_like_game_disc(listing_text):
        item["item_type"] = "game"


def guard_unsafe_extracted_item(item: dict[str, Any], *, listing_text: str) -> None:
    if not listing_text:
        return
    item_name = item.get("name") or ""
    canonical = item.get("canonical_name") or ""
    catalog_id = item.get("catalog_item_id") or item.get("catalog_entry_id")
    if item_is_console(item) and listing_looks_like_game_disc(listing_text) and not listing_has_strong_console_evidence(listing_text):
        clear_unsafe_catalog_match(
            item,
            listing_text=listing_text,
            reason="console_match_rejected_for_game_disc_listing",
        )
        return
    if distinctive_tokens(listing_text) and item_name and distinctive_tokens(item_name) and not has_distinctive_overlap(item_name, listing_text):
        clear_unsafe_catalog_match(
            item,
            listing_text=listing_text,
            reason="llm_item_name_not_found_in_listing_text",
        )
        return
    if (
        distinctive_tokens(listing_text)
        and canonical
        and catalog_id
        and distinctive_tokens(canonical)
        and not has_distinctive_overlap(canonical, f"{item_name}\n{listing_text}")
    ):
        clear_unsafe_catalog_match(
            item,
            listing_text=listing_text,
            reason="catalog_match_not_supported_by_listing_text",
        )


def normalize_extracted_items(items: list[dict[str, Any]], *, listing: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    listing_text = listing_guard_text(listing or {})
    result: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        raw_text = " ".join(
            str(raw.get(key) or "")
            for key in (
                "name",
                "title",
                "canonical_name",
                "item_type",
                "type",
                "price_source_text",
                "price_explanation",
                "missing_data_reason",
                "buyer_thoughts",
                "notes",
                "extraction_status",
            )
        ).lower().replace("ё", "е")
        raw_item_type = str(raw.get("item_type") or raw.get("type") or "").strip().lower()
        if raw_item_type in {
            "digital_account",
            "subscription",
            "service",
        }:
            continue
        if raw.get("is_physical") is False:
            continue
        if raw_item_type not in {"console", "game_console"} and any(
            marker in raw_text
            for marker in (
                "скупка",
                "выкуп",
                "куплю",
                "оценю",
                "принимаю",
                "обменяю ваш",
                "ремонт",
                "услуга",
                "услуги",
                "создание профиля",
                "создам профиль",
                "пополнение",
                "активация",
                "подписка",
                "аренда",
                "прокат",
                "аккаунт",
                "цифров",
                "digital",
                "key",
                "ключ",
                "прошивка",
                "blocked_by_junk_listing",
            )
        ):
            continue
        name = str(raw.get("name") or raw.get("title") or raw.get("canonical_name") or "").strip()
        if not name:
            continue
        quantity = int(safe_float(raw.get("quantity")) or 1)
        quantity = max(1, quantity)
        explicit_price = first_price(raw, "buy_price_used", "buy_price", "item_price", "price", "cost")
        item = {
            "name": name,
            "canonical_name": str(raw.get("canonical_name") or "").strip() or None,
            "catalog_item_id": str(raw.get("catalog_item_id") or "").strip() or None,
            "catalog_entry_id": int(safe_float(raw.get("catalog_entry_id")) or 0) or None,
            "quantity": quantity,
            "platform": raw.get("platform") or raw.get("platform_or_model"),
            "item_type": raw.get("item_type") or raw.get("type"),
            "buy_price_used": explicit_price,
            "has_explicit_buy_price": explicit_price is not None,
            "buy_price_reason": None,
            "unsafe_match_reason": None,
        }
        guard_unsafe_extracted_item(item, listing_text=listing_text)
        result.append(item)
    return dedupe_console_items(result)


def dedupe_console_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    console_by_generation: dict[str, dict[str, Any]] = {}
    for item in items:
        generation = console_generation_for_item(item)
        if not generation:
            result.append(item)
            continue
        existing = console_by_generation.get(generation)
        if existing is None:
            console_by_generation[generation] = item
            result.append(item)
            continue
        preferred = preferred_console_item(existing, item)
        if preferred is existing:
            continue
        console_by_generation[generation] = preferred
        result[result.index(existing)] = preferred
    return result


def console_generation_for_item(item: dict[str, Any]) -> str | None:
    item_type = str(item.get("item_type") or item.get("type") or "").strip().lower()
    text = normalize_lookup_text(
        " ".join(str(item.get(key) or "") for key in ("name", "canonical_name", "catalog_item_id"))
    )
    if item_type not in {"console", "game_console"} and not re.search(r"\b(ps4|ps5|playstation\s*[45])\b", text):
        return None
    if re.search(r"\b(ps5|playstation\s*5)\b", text):
        return "ps5"
    if re.search(r"\b(ps4|playstation\s*4)\b", text):
        return "ps4"
    return None


def preferred_console_item(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    def score(item: dict[str, Any]) -> tuple[int, int, int]:
        text = normalize_lookup_text(
            " ".join(str(item.get(key) or "") for key in ("name", "canonical_name", "catalog_item_id"))
        )
        exact = 1 if item.get("catalog_entry_id") else 0
        specific = sum(
            1
            for marker in ("slim", "pro", "fat", "digital", "500", "1tb", "1 tb", "1000")
            if marker in text
        )
        priced = 1 if item.get("buy_price_used") is not None else 0
        return (exact, specific, priced)

    return right if score(right) > score(left) else left


def console_account_subscription_bonus(
    *,
    listing: dict[str, Any],
    raw_json: dict[str, Any],
    source_items: list[dict[str, Any]],
    normalized_items: list[dict[str, Any]],
) -> dict[str, Any] | None:
    has_console = any(str(item.get("item_type") or "").lower() == "console" for item in normalized_items)
    if not has_console:
        return None

    raw_items = []
    if isinstance(source_items, list):
        raw_items.extend(item for item in source_items if isinstance(item, dict))
    raw_llm_items = raw_json.get("llm_extracted_items")
    if isinstance(raw_llm_items, list):
        raw_items.extend(item for item in raw_llm_items if isinstance(item, dict))

    text_parts: list[str] = [
        str(listing.get("title") or ""),
        str(listing.get("description") or ""),
        str(raw_json.get("description") or ""),
    ]
    for item in raw_items:
        text_parts.extend(
            str(item.get(key) or "")
            for key in (
                "name",
                "title",
                "canonical_name",
                "item_type",
                "price_source_text",
                "price_explanation",
                "missing_data_reason",
                "buyer_thoughts",
                "notes",
            )
        )
    text = normalize_lookup_text(" ".join(text_parts))
    if not text:
        return None

    account_markers = (
        "аккаунт",
        "профиль",
        "ps plus",
        "playstation plus",
        "plus extra",
        "plus deluxe",
        "делюкс",
        "экстра",
        "подписка",
        "подпиской",
        "игры на аккаунте",
        "игры в аккаунте",
        "до 2027",
        "до 2028",
    )
    if not any(marker in text for marker in account_markers):
        return None

    amount = 300.0
    name = "Account/subscription bundled with console"
    if any(marker in text for marker in ("deluxe", "делюкс", "premium", "extra", "экстра")):
        amount = 500.0
        name = "PlayStation Plus Extra/Deluxe bundled with console"
    elif any(marker in text for marker in ("ps plus", "playstation plus", "подписка")):
        amount = 400.0
        name = "PlayStation Plus bundled with console"

    if any(marker in text for marker in ("до 2027", "до 2028", "май 2027", "24 мая")):
        amount = max(amount, 500.0)

    return {
        "amount": amount,
        "name": name,
        "risk": "account_subscription_bundle_low_confidence_bonus",
        "reason": (
            "Small manual bundle bonus for account/subscription included with a physical console; "
            "not valued as a separately resellable digital product. Check account transfer and subscription rules."
        ),
    }


def distribute_buy_prices(items: list[dict[str, Any]], listing_price: float, delivery_price: float, *, has_lot_costs: bool = False) -> None:
    if not items:
        return
    total_units = sum(item["quantity"] for item in items)
    if has_lot_costs:
        explicit_total = sum((item["buy_price_used"] or 0.0) * item["quantity"] for item in items if item["has_explicit_buy_price"])
        missing_units = sum(item["quantity"] for item in items if not item["has_explicit_buy_price"])
        distributed = max(0.0, listing_price - explicit_total) / missing_units if missing_units else 0.0
        delivery_per_unit = delivery_price / total_units if total_units else 0.0
        for item in items:
            base_price = item["buy_price_used"] if item["buy_price_used"] is not None else distributed
            item["buy_price_used"] = base_price + delivery_per_unit
            item["has_explicit_buy_price"] = True
            if item.get("buy_price_reason") and delivery_per_unit:
                item["buy_price_reason"] = f"{item['buy_price_reason']} + доставка {round_money(delivery_per_unit)} ₽"
            elif item.get("buy_price_reason"):
                item["buy_price_reason"] = str(item["buy_price_reason"])
            elif delivery_per_unit:
                item["buy_price_reason"] = f"Цена закупки распределена из лота + доставка {round_money(delivery_per_unit)} ₽"
            else:
                item["buy_price_reason"] = "Цена закупки распределена из лота"
        return

    buy_total = listing_price + delivery_price
    explicit_total = sum((item["buy_price_used"] or 0.0) * item["quantity"] for item in items if item["has_explicit_buy_price"])
    missing_units = sum(item["quantity"] for item in items if not item["has_explicit_buy_price"])
    distributed = max(0.0, buy_total - explicit_total) / missing_units if missing_units else 0.0
    for item in items:
        if item["buy_price_used"] is None:
            item["buy_price_used"] = distributed


def allocate_bundle_buy_prices_by_market_value(
    response_items: list[dict[str, Any]],
    buy_total: float,
    risks: list[str],
) -> None:
    """Allocate whole-lot buy cost only across priced components.

    Unknown items and low-confidence leftovers must not get fake equal shares
    like "3750 ₽". The listing's full buy price is still counted against the
    deal, but item-level cost is assigned proportionally to market value of
    components that CRM could price.
    """

    priced_items = [
        item
        for item in response_items
        if safe_float(item.get("expected_sell_price")) is not None
        and safe_float(item.get("expected_sell_price")) > 0
        and safe_float(item.get("quantity")) is not None
        and safe_float(item.get("quantity")) > 0
    ]
    if not priced_items:
        risks.append("lot_buy_cost_not_allocated_no_priced_items")
        return

    total_market_weight = sum(
        float(item["expected_sell_price"]) * float(item.get("quantity") or 1)
        for item in priced_items
    )
    if total_market_weight <= 0:
        risks.append("lot_buy_cost_not_allocated_zero_market_weight")
        return

    for item in priced_items:
        quantity = float(item.get("quantity") or 1)
        item_weight = float(item["expected_sell_price"]) * quantity
        allocated_total = buy_total * item_weight / total_market_weight
        unit_buy_price = round_money(allocated_total / quantity)
        item["buy_price"] = unit_buy_price
        item["buy_price_used"] = unit_buy_price
        expected_net = safe_float(item.get("expected_net"))
        item["expected_profit"] = round_money(expected_net - unit_buy_price) if expected_net is not None else None
        reason = str(item.get("reason") or "").strip()
        weighted_reason = "себестоимость распределена по рыночному весу распознанных товаров"
        item["reason"] = f"{reason}; {weighted_reason}" if reason else weighted_reason

    if len(priced_items) < len(response_items):
        risks.append("profit_excludes_unpriced_items")


def payload_price(payload: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = parse_price(payload.get(key))
        if value is not None:
            return value
    raw_json = payload.get("raw_json")
    if isinstance(raw_json, dict):
        for key in keys:
            value = parse_price(raw_json.get(key))
            if value is not None:
                return value
    return None


def payload_delivery_price(payload: dict[str, Any]) -> float | None:
    return payload_price(payload, "delivery_price_rub", "delivery_price", "delivery")


def first_price(payload: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = parse_price(payload.get(key))
        if value is not None:
            return value
    return None


def observation_match_quality(name: str, observation: PriceObservation) -> float:
    needle = normalize_lookup_text(name)
    if not needle:
        return 0.0
    values = [observation.item_title or ""]
    raw_json = observation.raw_json if isinstance(observation.raw_json, dict) else {}
    values.extend(str(raw_json.get(key) or "") for key in ("matched_name", "canonical_name", "title"))
    scores = [text_match_quality(needle, normalize_lookup_text(value)) for value in values]
    return max(scores, default=0.0)


def text_match_quality(needle: str, candidate: str) -> float:
    if not needle or not candidate:
        return 0.0
    if needle == candidate:
        return 1.0
    if needle in candidate or candidate in needle:
        return 0.78
    needle_tokens = set(needle.split())
    candidate_tokens = set(candidate.split())
    if not needle_tokens or not candidate_tokens:
        return 0.0
    overlap = len(needle_tokens & candidate_tokens) / len(needle_tokens)
    return 0.62 if overlap >= 0.75 else 0.0


def alias_score(name: str, alias: str, weight: int) -> int:
    if name == alias:
        return 100_000 + weight
    if len(alias) >= 4 and (f" {alias} " in f" {name} " or alias in name or name in alias):
        return 50_000 + weight + len(alias)
    if text_match_quality(name, alias) > 0:
        return 10_000 + weight + len(alias)
    return 0


def weighted_median(candidates: list[PriceCandidate]) -> float:
    rows = sorted(candidates, key=lambda candidate: candidate.price)
    total = sum(candidate.weight for candidate in rows)
    if total <= 0:
        return rows[len(rows) // 2].price
    midpoint = total / 2
    cumulative = 0.0
    for candidate in rows:
        cumulative += candidate.weight
        if cumulative >= midpoint:
            return candidate.price
    return rows[-1].price


def trim_count_for_candidates(candidates: list[PriceCandidate]) -> int:
    if len(candidates) < PRICE_TRIM_MIN_COUNT:
        return 0
    return max(1, int(len(candidates) * PRICE_TRIM_PERCENT))


def trim_price_candidates(candidates: list[PriceCandidate]) -> list[PriceCandidate]:
    trim_count = trim_count_for_candidates(candidates)
    if trim_count <= 0:
        return candidates
    rows = sorted(candidates, key=lambda candidate: candidate.price)
    if len(rows) - (trim_count * 2) < 3:
        return candidates
    return rows[trim_count : len(rows) - trim_count]


def recency_weight(value: datetime | None) -> float:
    if value is None:
        return 0.75
    age_days = max(0, (datetime.now(timezone.utc).replace(tzinfo=None) - value).days)
    if age_days <= 14:
        return 1.0
    if age_days <= 30:
        return 0.85
    if age_days <= 60:
        return 0.65
    if age_days <= 90:
        return 0.5
    return 0.35


def market_quality_weight(observation: PriceObservation, raw_json: dict[str, Any]) -> float:
    if observation.observation_type != OBS_AVITO_MARKET:
        return 1.0
    weight = 1.0
    context = raw_json.get("monitor_context")
    if isinstance(context, dict):
        cycle_number = safe_int(context.get("cycle_number"))
        bootstrap_all = context.get("bootstrap_all")
        if bootstrap_all is True or cycle_number == 1:
            weight *= 0.65
        elif cycle_number and cycle_number > 1:
            weight *= 1.05
        reservation = normalize_lookup_text(str(raw_json.get("reservation_status") or ""))
        if reservation in {"reserved", "bron", "booked", "в брони", "забронировано"} and bootstrap_all is not True:
            weight *= 1.35
    if observation_is_reserved(raw_json):
        weight *= 1.15
    listing_age = listing_lifetime_days(raw_json)
    if listing_age is not None:
        if listing_age >= 14:
            weight *= 0.45
        elif listing_age >= 7:
            weight *= 0.6
        elif listing_age >= 3:
            weight *= 0.8
    posted_age = listing_posted_age_days(raw_json)
    if posted_age is not None:
        if posted_age >= 14:
            weight *= 0.5
        elif posted_age >= 7:
            weight *= 0.7
        elif posted_age >= 3:
            weight *= 0.85
    return max(0.2, min(weight, 1.6))


def observation_is_reserved(raw_json: dict[str, Any]) -> bool:
    if raw_json.get("is_reserved") is True:
        return True
    reservation = normalize_lookup_text(str(raw_json.get("reservation_status") or ""))
    return reservation in {
        "reserved",
        "bron",
        "booked",
        "in reserve",
        "в брони",
        "забронировано",
        "зарезервировано",
        "бронь",
        "РІ Р±СЂРѕРЅРё",
        "Р·Р°Р±СЂРѕРЅРёСЂРѕРІР°РЅРѕ",
    }


def listing_lifetime_days(raw_json: dict[str, Any]) -> int | None:
    context = raw_json.get("monitor_context")
    if not isinstance(context, dict):
        return None
    first_seen = parse_observation_datetime(context.get("first_seen_at") or context.get("cycle_started_at"))
    last_seen = parse_observation_datetime(context.get("last_seen_at"))
    if first_seen is None or last_seen is None:
        return None
    return max(0, (last_seen - first_seen).days)


def listing_posted_age_days(raw_json: dict[str, Any]) -> int | None:
    posted_at = parse_observation_datetime(raw_json.get("posted_at"))
    if posted_at is None:
        return None
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return max(0, (now - posted_at).days)


def parse_observation_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def safe_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def observation_within_days(observation: CachedPriceObservation, days: int) -> bool:
    if observation.observed_at is None:
        return days >= EXTENDED_MARKET_DAYS
    age_days = max(0, (datetime.now(timezone.utc).replace(tzinfo=None) - observation.observed_at).days)
    return age_days <= days


def confidence_label(total_weight: float, max_confidence: float, count: int) -> str:
    if total_weight >= 1.0 and max_confidence >= 0.8 and count >= 2:
        return "high"
    if total_weight >= 0.45 or max_confidence >= 0.7:
        return "medium"
    return "low"


def fallback_allowed_types(name: str, item_type: str | None) -> set[str]:
    normalized_type = normalize_lookup_text(str(item_type or ""))
    normalized_name = normalize_lookup_text(name)
    if "console" in normalized_type or "пристав" in normalized_type:
        return {"console"}
    if "controller" in normalized_type or "геймпад" in normalized_type or "джоист" in normalized_type:
        return {"controller"}
    if any(term in normalized_name for term in ("dualshock", "dualsense", "геймпад", "джоистик", "джостик")):
        return {"controller"}
    if any(term in normalized_name for term in ("playstation 4", "playstation 5", "ps4", "ps5", "пс4", "пс5")) and any(
        term in normalized_name for term in ("slim", "fat", "pro", "500", "1tb", "1тб", "консоль", "пристав")
    ):
        return {"console"}
    return {"game"}


def platform_conflicts(left: Any, right: Any) -> bool:
    left_family = platform_family(left)
    right_family = platform_family(right)
    return bool(left_family and right_family and left_family != right_family)


def platform_family(value: Any) -> str | None:
    text = normalize_lookup_text(str(value or ""))
    if any(token in text for token in ("playstation 5", "ps5", "пс5")):
        return "ps5"
    if any(token in text for token in ("playstation 4", "ps4", "пс4")):
        return "ps4"
    if " 5 " in f" {text} ":
        return "ps5"
    if " 4 " in f" {text} ":
        return "ps4"
    return None


def observation_canonical_name(observation: PriceObservation) -> str:
    raw_json = observation.raw_json if isinstance(observation.raw_json, dict) else {}
    return str(raw_json.get("matched_name") or raw_json.get("canonical_name") or observation.item_title)


def observation_item_type(observation: PriceObservation, fallback: Any) -> str:
    raw_json = observation.raw_json if isinstance(observation.raw_json, dict) else {}
    return str(raw_json.get("item_type") or fallback or "game")


def observation_reason(observation: PriceObservation) -> str:
    raw_json = observation.raw_json if isinstance(observation.raw_json, dict) else {}
    if raw_json.get("source_kind") == "manual_market_baseline":
        return "ручной baseline Кирилла"
    if observation.observation_type == OBS_OWN_SALE:
        return "цена по твоей реальной продаже"
    if observation.observation_type == OBS_PARTNER_SALE:
        return "цена по партнёрской продаже"
    if observation.observation_type == OBS_OWN_AVITO_ACTIVE:
        return "цена по твоему активному объявлению"
    if observation.observation_type == OBS_AVITO_MARKET:
        return "проверенное рыночное объявление Авито"
    return "цена по CRM-базе"


def listing_decision(
    *,
    matched_count: int,
    total_count: int,
    expected_profit: float,
    profit_percent: float | None,
    risks: list[str],
    fallback_only: bool = False,
) -> str:
    if matched_count == 0 or "listing_price_missing" in risks:
        return "skip"
    if any(risk in risks for risk in ("digital_product", "rental", "subscription", "console_storage_missing")):
        return "skip"
    if expected_profit <= 0:
        return "skip"
    if expected_profit >= 1_000 and (profit_percent or 0) >= 35 and matched_count == total_count and not fallback_only:
        return "good"
    if expected_profit >= 300 and (profit_percent or 0) >= 15:
        return "check"
    return "skip"


def listing_summary(decision: str, matched_count: int, expected_profit: float) -> str:
    if decision == "good":
        return f"Можно смотреть: {matched_count} поз. распознано, ожидаемый профит около {money_text(expected_profit)}."
    if decision == "check":
        return f"Нужно проверить: {matched_count} поз. распознано, примерный профит {money_text(expected_profit)}."
    return f"Лучше пропустить: {matched_count} поз. распознано, примерный профит {money_text(expected_profit)}."


def round_price(value: float) -> float:
    return round_money(round(value / 50) * 50)


def round_money(value: float | None) -> float | None:
    if value is None:
        return None
    return float(round(value))


def money_text(value: float | None) -> str:
    if value is None:
        return "неизвестно"
    return f"{round(value):,} ₽".replace(",", " ")

