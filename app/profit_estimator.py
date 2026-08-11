from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .monitor import NewListingRecord


ROOT = Path(__file__).resolve().parents[1]
CRM_GAME_SUGGESTIONS_PATH = ROOT / "data" / "crm" / "crm_game_suggestions.json"
_CRM_GAME_CACHE: list[dict[str, object]] | None = None


def normalize_text(value: str | None) -> str:
    value = (value or "").lower().replace("ё", "е")
    value = re.sub(r"[^0-9a-zа-я]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def contains_alias(normalized_text: str, alias: str) -> bool:
    normalized_alias = normalize_text(alias)
    if not normalized_alias:
        return False
    return f" {normalized_alias} " in f" {normalized_text} "


def load_price_map(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def estimate_profit(record: NewListingRecord, price_map: dict[str, Any], settings: dict[str, object] | None = None) -> dict[str, object]:
    title_text = normalize_text(record.title or "")
    text = normalize_text("\n".join([record.title or "", record.description or "", *record.raw_card_texts]))
    matches = match_games(text, price_map)
    explicit_count = explicit_disc_count(text)
    has_disc_mention = mentions_physical_games(text)
    unknown_disc_avg = int(price_map.get("unknown_disc_avg_price_rub", 700))
    disc_count = max(len(matches), explicit_count or 0)
    has_console = looks_like_console(text)
    scenario = choose_scenario(matches, disc_count, has_console)
    if has_disc_mention and not explicit_count and not matches and not has_console:
        scenario = "unknown_discs"
    if is_excluded_listing(title_text):
        scenario = "excluded_service_or_rental"
    resale_total = sum(int(item["price"]) for item in matches)
    per_disc_cost = int(price_map.get("default_per_disc_cost_rub", 100))
    unknown_disc_count = unknown_disc_count_from(explicit_count, len(matches), has_disc_mention)
    known_unknown_count = unknown_disc_count if isinstance(unknown_disc_count, int) else 0
    unknown_disc_total = known_unknown_count * unknown_disc_avg
    valuation_total = resale_total + unknown_disc_total
    handling_cost = per_disc_cost * (len(matches) + known_unknown_count)
    avito_price = int(record.price or 0)
    delivery_price = extract_delivery_price_rub(
        "\n".join(
            [
                record.delivery_text or "",
                "\n".join(record.raw_card_texts or []),
                "\n".join(record.raw_detail_texts or []),
            ]
        )
    )
    total_buy_price = avito_price + int(delivery_price or 0)
    risks: list[str] = []
    if delivery_price is None:
        risks.append("delivery_price_unknown")

    result: dict[str, object] = {
        "scenario": scenario,
        "listing_type": scenario,
        "matched_games": matches,
        "matched_count": len(matches),
        "disc_count_hint": disc_count or len(matches),
        "resale_total_rub": resale_total,
        "valuation_total_rub": valuation_total,
        "handling_cost_rub": handling_cost,
        "avito_price_rub": record.price,
        "listing_price_rub": record.price,
        "delivery_price_rub": delivery_price,
        "total_buy_price_rub": total_buy_price if record.price is not None else None,
        "risks": risks,
        "unknown_disc_avg_price_rub": unknown_disc_avg,
        "unknown_disc_count": unknown_disc_count,
        "unknown_disc_total_rub": unknown_disc_total if known_unknown_count else None,
        "needs_manual_review": bool(unknown_disc_count),
        "manual_review_reason": manual_review_reason(unknown_disc_count),
        "estimated_profit_rub": None,
        "is_interesting_by_profit": "unclear",
        "notes": "",
    }

    if has_game_profit_rules(settings):
        lot_result = estimate_game_lot_if_applicable(
            result=result,
            record=record,
            normalized_text=text,
            matches=matches,
            explicit_count=explicit_count,
            has_disc_mention=has_disc_mention,
            settings=settings,
        )
        if lot_result is not None:
            return lot_result

    if scenario == "excluded_service_or_rental":
        result["is_interesting_by_profit"] = "no"
        result["notes"] = "Стоп-тип: аренда, прокат, скупка, выкуп или сервис вместо продажи товара."
        return result

    if scenario == "console_only" and record.price:
        model = console_model(text)
        limits = price_map.get("console_limits_rub", {})
        limit = int(limits.get(model, 0)) if isinstance(limits, dict) else 0
        discount_to_limit = limit - avito_price if limit else None
        result.update(
            {
                "console_model": model,
                "net_console_price_rub": avito_price,
                "console_limit_rub": limit,
                "estimated_profit_rub": discount_to_limit,
                "is_interesting_by_profit": "yes" if limit and avito_price <= limit else "no",
                "notes": "Только приставка: сравниваем цену Авито с закупочным лимитом модели.",
            }
        )
        return result

    if scenario == "unknown_discs":
        result["notes"] = "В объявлении упомянуты диски, но не указано понятное количество и названия игр."
        return result

    if (not matches and not known_unknown_count) or not record.price:
        result["notes"] = "Не нашёл игр из прайса друга в названии/описании."
        return result

    estimated_profit = valuation_total - avito_price - handling_cost
    result["estimated_profit_rub"] = estimated_profit

    if scenario == "single_game":
        if has_game_profit_rules(settings):
            apply_single_game_quick_sell_rules(result, matches, record, settings)
        if result.get("quick_sell_rules_applied"):
            return result
        margin_ratio = estimated_profit / avito_price if avito_price else 0
        result["margin_ratio"] = round(margin_ratio, 3)
        result["is_interesting_by_profit"] = "unclear" if known_unknown_count else ("yes" if margin_ratio >= 0.4 else "no")
        result["notes"] = (
            "Один диск не распознан: считаю по средней 700 ₽, но надо посмотреть что за игра."
            if known_unknown_count
            else "Один диск: профит = цена друга - цена Авито - 100 ₽."
        )
        return result

    if scenario == "game_bundle":
        multiplier = float(price_map.get("bundle_buy_multiplier", 0.5))
        buy_threshold = int(valuation_total * multiplier)
        result["buy_threshold_rub"] = buy_threshold
        result["is_interesting_by_profit"] = "unclear" if known_unknown_count else ("yes" if avito_price <= buy_threshold else "no")
        result["notes"] = (
            "Пачка дисков: часть игр не распознана, неизвестные диски считаю по 700 ₽ за штуку."
            if known_unknown_count
            else "Пачка дисков: интересна, если цена лота <= 50% суммы по прайсу."
        )
        return result

    if scenario == "console_bundle":
        multiplier = float(price_map.get("console_bundle_disc_multiplier", 0.5))
        disc_value = int(valuation_total * multiplier)
        extra_controllers = max(0, estimate_controller_count(text) - 1)
        controller_value = extra_controllers * int(price_map.get("extra_controller_value_rub", 1000))
        model = console_model(text)
        limits = price_map.get("console_limits_rub", {})
        limit = int(limits.get(model, 0)) if isinstance(limits, dict) else 0
        net_console_price = avito_price - disc_value - controller_value
        result.update(
            {
                "console_model": model,
                "disc_buy_value_rub": disc_value,
                "extra_controllers": extra_controllers,
                "extra_controller_value_rub": controller_value,
                "net_console_price_rub": net_console_price,
                "console_limit_rub": limit,
                "is_interesting_by_profit": "unclear" if known_unknown_count else ("yes" if limit and net_console_price <= limit else "no"),
                "notes": (
                    "Комплект: часть дисков не распознана, неизвестные диски считаю по 700 ₽ за штуку."
                    if known_unknown_count
                    else "Комплект: цена приставки = Авито - 50% цены дисков - доп. джойстики."
                ),
            }
        )
        return result

    return result


def estimate_game_lot_if_applicable(
    *,
    result: dict[str, object],
    record: NewListingRecord,
    normalized_text: str,
    matches: list[dict[str, object]],
    explicit_count: int | None,
    has_disc_mention: bool,
    settings: dict[str, object] | None,
) -> dict[str, object] | None:
    if is_digital_or_non_physical(normalized_text):
        result.update(
            {
                "scenario": "digital_account",
                "listing_type": "digital_account",
                "decision": "SKIP",
                "lot_profit_decision": "SKIP",
                "is_interesting_by_profit": "no",
                "estimated_profit_rub": None,
                "expected_profit_rub": None,
                "lot_expected_profit": None,
                "decision_reason": "цифровой аккаунт / аренда / не физические диски",
                "notes": "цифровой аккаунт / аренда / не физические диски",
            }
        )
        return result

    if result.get("scenario") == "single_game" and len(matches) <= 1 and not explicit_count:
        return None

    lot_like = is_lot_like(normalized_text, matches, explicit_count, has_disc_mention)
    if not lot_like:
        return None

    individual_items = extract_individual_price_lot_items(record, matches, settings)
    if individual_items:
        return apply_individual_price_lot_rules(result, individual_items, record, settings)

    if has_ambiguous_lot_price_markers(normalized_text):
        return mark_lot_unknown_structure(result, "лот игр, структура цены непонятна")

    known_count = len(matches)
    unknown_count = max(0, (explicit_count or known_count) - known_count)
    if known_count == 0:
        return mark_lot_unknown_structure(result, "игры не сопоставлены с базой")
    if explicit_count is None and has_disc_mention and known_count <= 1:
        return mark_lot_unknown_structure(result, "похоже на лот PS4-игр, требуется ручная проверка")

    return apply_total_price_lot_rules(result, matches, unknown_count, record, settings)


def is_digital_or_non_physical(normalized_text: str) -> bool:
    return any(
        contains_alias(normalized_text, token)
        for token in (
            "аккаунт",
            "цифровой",
            "цифровая версия",
            "аренда",
            "прокат",
            "primary",
            "secondary",
            "прошивка",
            "goldhen",
            "jailbreak",
            "p1",
            "p2",
            "p3",
            "п1",
            "п2",
            "п3",
        )
    )


def is_lot_like(
    normalized_text: str,
    matches: list[dict[str, object]],
    explicit_count: int | None,
    has_disc_mention: bool,
) -> bool:
    if len(matches) > 1:
        return True
    if explicit_count and explicit_count > 1:
        return True
    phrases = (
        "игры ps4",
        "игры на ps4",
        "диски ps4",
        "диски на ps4",
        "игры playstation 4",
        "диски playstation 4",
        "комплект игр",
        "пачка игр",
        "несколько игр",
        "продам игры",
        "список игр",
        "ps4 games",
        "games ps4",
        "ps4 discs",
        "bundle games",
        "game bundle",
    )
    if any(contains_alias(normalized_text, phrase) for phrase in phrases):
        return True
    return has_disc_mention and len(matches) > 1


def has_ambiguous_lot_price_markers(normalized_text: str) -> bool:
    phrases = (
        "цена от",
        "от 500",
        "за одну",
        "за 1 игру",
        "цены разные",
        "разные цены",
        "спрашивайте",
        "спрашивайте цену",
        "цену уточняйте",
        "цена в описании",
        "не все цены",
    )
    return any(contains_alias(normalized_text, phrase) for phrase in phrases)


def mark_lot_unknown_structure(result: dict[str, object], reason: str) -> dict[str, object]:
    result.update(
        {
            "scenario": "game_lot_unknown_structure",
            "listing_type": "game_lot_unknown_structure",
            "decision": "CHECK",
            "lot_profit_decision": "uncertain",
            "is_interesting_by_profit": "unclear",
            "estimated_profit_rub": None,
            "expected_profit_rub": None,
            "lot_expected_profit": None,
            "lot_reasons": [reason],
            "decision_reason": reason,
            "notes": reason,
            "needs_manual_review": True,
        }
    )
    return result


def quick_sell_values_for_game(game: dict[str, object], settings: dict[str, object] | None) -> dict[str, object]:
    rules = game_profit_settings(settings)
    base_shop_price = int(game.get("price") or 0)
    price_confidence = game_price_confidence(game, rules)
    if price_confidence == "missing" or base_shop_price <= 0:
        return {
            "base_shop_price_rub": base_shop_price,
            "price_confidence": "missing",
            "quick_sell_price_rub": None,
            "sale_commission_amount_rub": None,
            "fixed_cost_per_disc_rub": int(rules.get("fixed_cost_per_disc") or 50),
            "min_profit_per_disc_rub": int(rules.get("min_profit_per_disc") or 300),
        }
    discount_percent = float(rules.get("discount_from_shop_median_percent") or 20)
    commission_percent = float(rules.get("sale_commission_percent") or 9)
    fixed_cost = int(rules.get("fixed_cost_per_disc") or 50)
    min_profit = int(rules.get("min_profit_per_disc") or 300)
    step = int(rules.get("round_price_step_rub") or 50)
    quick_round_mode = str(rules.get("round_quick_sell_price") or "nearest")
    quick_sell = round_to_step(base_shop_price * (1 - discount_percent / 100), step, quick_round_mode)
    commission = quick_sell * commission_percent / 100
    return {
        "base_shop_price_rub": base_shop_price,
        "price_confidence": price_confidence,
        "quick_sell_price_rub": quick_sell,
        "sale_commission_percent": commission_percent,
        "sale_commission_amount_rub": round(commission, 2),
        "fixed_cost_per_disc_rub": fixed_cost,
        "min_profit_per_disc_rub": min_profit,
    }


def apply_total_price_lot_rules(
    result: dict[str, object],
    matches: list[dict[str, object]],
    unknown_count: int,
    record: NewListingRecord,
    settings: dict[str, object] | None,
) -> dict[str, object]:
    rules = game_profit_settings(settings)
    known_count = len(matches)
    values = [quick_sell_values_for_game(game, settings) for game in matches]
    quick_sell_sum = sum(int(item.get("quick_sell_price_rub") or 0) for item in values)
    commission_percent = float(rules.get("sale_commission_percent") or 9)
    fixed_cost = int(rules.get("fixed_cost_per_disc") or 50)
    min_profit = int(rules.get("min_profit_per_disc") or 300)
    lot_commission = quick_sell_sum * commission_percent / 100
    lot_fixed_cost = fixed_cost * known_count
    total_buy = int(result.get("total_buy_price_rub") or record.price or 0)
    expected_profit = quick_sell_sum - lot_commission - lot_fixed_cost - total_buy
    risks = list(result.get("risks") or [])
    reasons: list[str] = []

    if unknown_count > 0:
        risks.append("часть игр не найдена в базе")
        decision = "CHECK"
        reason = "часть игр не найдена в базе"
    elif expected_profit >= min_profit:
        decision = "GOOD"
        reason = "профит лота выше минимума"
    elif expected_profit > 0:
        decision = "CHECK"
        reason = "профит положительный, но ниже минимума"
    else:
        decision = "SKIP"
        reason = "профит нулевой или отрицательный"
    reasons.append(reason)

    lot_items = [
        {
            "raw_name": game.get("name"),
            "normalized_game_name": game.get("name"),
            "matched": True,
            "item_price": None,
            "quick_sell_price": values[index].get("quick_sell_price_rub"),
            "price_confidence": values[index].get("price_confidence"),
            "expected_profit": None,
            "decision": None,
            "risks": [],
        }
        for index, game in enumerate(matches)
    ]
    for _ in range(unknown_count):
        lot_items.append(
            {
                "raw_name": None,
                "normalized_game_name": None,
                "matched": False,
                "item_price": None,
                "quick_sell_price": None,
                "price_confidence": "missing",
                "expected_profit": None,
                "decision": "CHECK",
                "risks": ["game_not_matched"],
            }
        )

    result.update(
        {
            "scenario": "game_lot_total_price",
            "listing_type": "game_lot_total_price",
            "decision": decision,
            "lot_profit_decision": decision,
            "decision_reason": reason,
            "is_interesting_by_profit": "yes" if decision == "GOOD" else ("no" if decision == "SKIP" else "unclear"),
            "lot_games_count": known_count + unknown_count,
            "lot_known_games_count": known_count,
            "lot_unknown_games_count": unknown_count,
            "lot_quick_sell_sum": quick_sell_sum,
            "lot_sale_commission_amount": round(lot_commission, 2),
            "lot_fixed_cost": lot_fixed_cost,
            "lot_expected_profit": round(expected_profit, 2),
            "estimated_profit_rub": int(round(expected_profit)),
            "expected_profit_rub": round(expected_profit, 2),
            "lot_reasons": reasons,
            "lot_risks": risks,
            "lot_items": lot_items,
            "risks": risks,
            "needs_manual_review": unknown_count > 0,
            "notes": reason,
        }
    )
    return result


def extract_individual_price_lot_items(
    record: NewListingRecord,
    matches: list[dict[str, object]],
    settings: dict[str, object] | None,
) -> list[dict[str, object]]:
    text = "\n".join([record.description or "", *record.raw_detail_texts, *record.raw_card_texts])
    items: list[dict[str, object]] = []
    seen_names: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        price_match = re.search(r"(.{2,120}?)[\s:–—-]+(\d{2,5})\s*(?:₽|руб|р\b)?\s*$", line, flags=re.IGNORECASE)
        if not price_match:
            continue
        raw_name = price_match.group(1).strip(" -–—:\t")
        item_price = int(price_match.group(2))
        normalized_line = normalize_text(raw_name)
        matched = next((game for game in matches if contains_alias(normalized_line, str(game.get("name") or ""))), None)
        if not matched:
            for game in matches:
                aliases = [str(game.get("name") or ""), *(str(alias) for alias in game.get("aliases", []) if isinstance(game.get("aliases"), list))]
                if any(contains_alias(normalized_line, alias) for alias in aliases):
                    matched = game
                    break
        if not matched:
            continue
        name = str(matched.get("name") or "")
        if name in seen_names:
            continue
        seen_names.add(name)
        values = quick_sell_values_for_game(matched, settings)
        quick_sell = values.get("quick_sell_price_rub")
        commission = values.get("sale_commission_amount_rub") or 0
        fixed_cost = values.get("fixed_cost_per_disc_rub") or 0
        risks: list[str] = []
        if quick_sell is None:
            expected_profit = None
            decision = "CHECK"
        else:
            expected_profit = float(quick_sell) - float(commission) - int(fixed_cost) - item_price
            min_profit = int(values.get("min_profit_per_disc_rub") or 300)
            if values.get("price_confidence") == "low":
                decision = "CHECK"
            elif expected_profit >= min_profit:
                decision = "GOOD"
            elif expected_profit > 0:
                decision = "CHECK"
            else:
                decision = "SKIP"
        items.append(
            {
                "raw_name": raw_name,
                "normalized_game_name": name,
                "matched": True,
                "item_price": item_price,
                "quick_sell_price": quick_sell,
                "price_confidence": values.get("price_confidence"),
                "expected_profit": round(expected_profit, 2) if expected_profit is not None else None,
                "decision": decision,
                "risks": risks,
            }
        )
    return items


def extract_bundle_total_price(record: NewListingRecord) -> int | None:
    text = "\n".join([record.description or "", *record.raw_detail_texts, *record.raw_card_texts]).replace("\xa0", " ")
    patterns = [
        r"(?:за\s*(?:все|всё|весь\s+лот|весь\s+комплект)|комплектом|все\s+вместе|всё\s+вместе|оптом)\D{0,30}(\d{3,6})\s*(?:₽|руб|р\b)?",
        r"(\d{3,6})\s*(?:₽|руб|р\b)?\D{0,30}(?:за\s*(?:все|всё|весь\s+лот|весь\s+комплект)|комплектом|все\s+вместе|всё\s+вместе|оптом)",
    ]
    candidates: list[int] = []
    for line in text.splitlines():
        for pattern in patterns:
            for match in re.finditer(pattern, line, flags=re.IGNORECASE):
                value = int(match.group(1).replace(" ", ""))
                if 100 <= value <= 300000:
                    candidates.append(value)
    return min(candidates) if candidates else None


def individual_lot_bundle_metrics(
    result: dict[str, object],
    lot_items: list[dict[str, object]],
    record: NewListingRecord,
    settings: dict[str, object] | None,
) -> dict[str, object]:
    item_price_sum = sum(int(item.get("item_price") or 0) for item in lot_items)
    quick_sell_sum = sum(int(item.get("quick_sell_price") or 0) for item in lot_items)
    bundle_total_price = extract_bundle_total_price(record)
    metrics: dict[str, object] = {
        "lot_individual_price_sum": item_price_sum,
        "lot_bundle_total_price": bundle_total_price,
        "lot_bundle_discount_rub": None,
        "lot_bundle_expected_profit": None,
        "lot_bundle_is_better_than_individual": False,
    }
    if bundle_total_price is None:
        return metrics
    rules = game_profit_settings(settings)
    delivery_price = int(result.get("delivery_price_rub") or 0)
    bundle_total_buy = bundle_total_price + delivery_price
    commission_percent = float(rules.get("sale_commission_percent") or 9)
    fixed_cost = int(rules.get("fixed_cost_per_disc") or 50) * len(lot_items)
    commission = quick_sell_sum * commission_percent / 100
    expected_profit = quick_sell_sum - commission - fixed_cost - bundle_total_buy
    metrics.update(
        {
            "lot_bundle_discount_rub": max(0, item_price_sum - bundle_total_price) if item_price_sum else None,
            "lot_bundle_expected_profit": round(expected_profit, 2),
            "lot_bundle_is_better_than_individual": bool(item_price_sum and bundle_total_price < item_price_sum),
        }
    )
    return metrics


def apply_individual_price_lot_rules(
    result: dict[str, object],
    lot_items: list[dict[str, object]],
    record: NewListingRecord,
    settings: dict[str, object] | None,
) -> dict[str, object]:
    risks = list(result.get("risks") or [])
    if result.get("delivery_price_rub") is not None:
        risks.append("delivery_not_allocated_in_individual_price_lot")
    decisions = [str(item.get("decision") or "") for item in lot_items]
    positive = any((item.get("expected_profit") or 0) > 0 for item in lot_items)
    bundle_metrics = individual_lot_bundle_metrics(result, lot_items, record, settings)
    bundle_profit = bundle_metrics.get("lot_bundle_expected_profit")
    if "GOOD" in decisions:
        decision = "GOOD"
        reason = "есть хотя бы одна игра с GOOD"
    elif isinstance(bundle_profit, (int, float)) and bundle_profit >= int(game_profit_settings(settings).get("min_profit_per_disc") or 300):
        decision = "GOOD"
        reason = "комплектом дешевле, профит по всему набору выше минимума"
    elif "CHECK" in decisions or positive:
        decision = "CHECK"
        reason = "есть игры для проверки или положительный профит"
    else:
        decision = "SKIP"
        reason = "все распознанные игры минусовые"
    total_profit = sum(float(item.get("expected_profit") or 0) for item in lot_items)
    if bundle_metrics.get("lot_bundle_is_better_than_individual") and isinstance(bundle_profit, (int, float)):
        total_profit = float(bundle_profit)
    result.update(
        {
            "scenario": "game_lot_individual_prices",
            "listing_type": "game_lot_individual_prices",
            "decision": decision,
            "lot_profit_decision": decision,
            "decision_reason": reason,
            "is_interesting_by_profit": "yes" if decision == "GOOD" else ("no" if decision == "SKIP" else "unclear"),
            "lot_games_count": len(lot_items),
            "lot_known_games_count": len(lot_items),
            "lot_unknown_games_count": 0,
            "lot_quick_sell_sum": sum(int(item.get("quick_sell_price") or 0) for item in lot_items),
            "lot_sale_commission_amount": None,
            "lot_fixed_cost": None,
            "lot_expected_profit": round(total_profit, 2),
            **bundle_metrics,
            "estimated_profit_rub": int(round(total_profit)),
            "expected_profit_rub": round(total_profit, 2),
            "lot_reasons": [reason],
            "lot_risks": risks,
            "lot_items": lot_items,
            "risks": risks,
            "notes": reason,
        }
    )
    return result


def has_game_profit_rules(settings: dict[str, object] | None) -> bool:
    if not isinstance(settings, dict):
        return False
    profit_rules = settings.get("profit_rules", {})
    return isinstance(profit_rules, dict) and isinstance(profit_rules.get("games"), dict)


def game_profit_settings(settings: dict[str, object] | None) -> dict[str, object]:
    defaults: dict[str, object] = {
        "discount_from_shop_median_percent": 20,
        "min_profit_per_disc": 300,
        "sale_commission_percent": 9,
        "fixed_cost_per_disc": 50,
        "round_price_step_rub": 50,
        "round_quick_sell_price": "nearest",
        "round_max_buy_price": "floor",
        "low_price_observation_threshold": 3,
    }
    if not isinstance(settings, dict):
        return defaults
    profit_rules = settings.get("profit_rules", {})
    if not isinstance(profit_rules, dict):
        return defaults
    games = profit_rules.get("games", {})
    if not isinstance(games, dict):
        return defaults
    merged = dict(defaults)
    merged.update(games)
    return merged


def round_to_step(value: float, step: int, mode: str) -> int:
    step = max(1, int(step or 50))
    if mode == "nearest":
        return int(((value / step) + 0.5) // 1 * step)
    return int(value // step * step)


def game_price_confidence(game: dict[str, object], rules: dict[str, object]) -> str:
    price = int(game.get("price") or 0)
    if price <= 0:
        return "missing"
    observations = game.get("price_observation_count")
    if observations is None:
        observations = game.get("observation_count")
    if isinstance(observations, str) and observations.isdigit():
        observations = int(observations)
    threshold = int(rules.get("low_price_observation_threshold") or 3)
    if isinstance(observations, int) and observations < threshold:
        return "low"
    return str(game.get("price_confidence") or "high")


def apply_single_game_quick_sell_rules(
    result: dict[str, object],
    matches: list[dict[str, object]],
    record: NewListingRecord,
    settings: dict[str, object] | None,
) -> None:
    if len(matches) != 1:
        return
    game = matches[0]
    rules = game_profit_settings(settings)
    base_shop_price = int(game.get("price") or 0)
    price_confidence = game_price_confidence(game, rules)
    result["price_confidence"] = price_confidence
    result["normalized_game_name"] = game.get("name")
    result["platform"] = "ps4"
    result["quick_sell_rules_applied"] = True

    if price_confidence == "missing" or base_shop_price <= 0:
        result.update(
            {
                "decision": "CHECK",
                "decision_reason": "нет ценовой базы",
                "is_interesting_by_profit": "unclear",
                "estimated_profit_rub": None,
            }
        )
        return

    discount_percent = float(rules.get("discount_from_shop_median_percent") or 20)
    commission_percent = float(rules.get("sale_commission_percent") or 9)
    fixed_cost = int(rules.get("fixed_cost_per_disc") or 50)
    min_profit = int(rules.get("min_profit_per_disc") or 300)
    step = int(rules.get("round_price_step_rub") or 50)
    quick_round_mode = str(rules.get("round_quick_sell_price") or "nearest")
    max_buy_round_mode = str(rules.get("round_max_buy_price") or "floor")

    raw_quick_sell = base_shop_price * (1 - discount_percent / 100)
    quick_sell = round_to_step(raw_quick_sell, step, quick_round_mode)
    commission = quick_sell * commission_percent / 100
    raw_max_buy = quick_sell - commission - fixed_cost - min_profit
    max_buy = round_to_step(raw_max_buy, step, max_buy_round_mode)
    total_buy = result.get("total_buy_price_rub")
    if total_buy is None:
        total_buy = int(record.price or 0)
    expected_profit = quick_sell - commission - fixed_cost - int(total_buy)

    if price_confidence == "low":
        decision = "CHECK"
        reason = "цена есть, но мало наблюдений"
        interest = "unclear"
    elif int(total_buy) <= max_buy:
        decision = "GOOD"
        reason = "цена покупки ниже max_buy_price"
        interest = "yes"
    elif expected_profit >= min_profit:
        decision = "GOOD"
        reason = "ожидаемый профит выше минимума"
        interest = "yes"
    elif expected_profit > 0:
        decision = "CHECK"
        reason = "профит положительный, но ниже минимума"
        interest = "unclear"
    else:
        decision = "SKIP"
        reason = "профит нулевой или отрицательный"
        interest = "no"

    result.update(
        {
            "base_shop_price_rub": base_shop_price,
            "discount_from_shop_median_percent": discount_percent,
            "quick_sell_price_rub": quick_sell,
            "sale_commission_percent": commission_percent,
            "sale_commission_amount_rub": round(commission, 2),
            "fixed_cost_per_disc_rub": fixed_cost,
            "min_profit_per_disc_rub": min_profit,
            "max_buy_price_rub": max_buy,
            "expected_profit_rub": round(expected_profit, 2),
            "estimated_profit_rub": int(round(expected_profit)),
            "decision": decision,
            "decision_reason": reason,
            "is_interesting_by_profit": interest,
            "notes": (
                "Один диск: quick_sell = цена магазина минус скидка; "
                "профит = quick_sell - комиссия - фикс. расход - цена покупки с доставкой."
            ),
        }
    )


def extract_delivery_price_rub(text: str | None) -> int | None:
    source = (text or "").replace("\xa0", " ")
    if not source:
        return None
    lowered = source.lower()
    if "достав" not in lowered and "delivery" not in lowered and "отправ" not in lowered:
        return None
    matches = re.findall(r"(?:от\s*)?(\d{1,5})\s*(?:₽|руб|р\b)", lowered)
    if not matches:
        return None
    values = [int(value) for value in matches]
    plausible = [value for value in values if 0 <= value <= 5000]
    return min(plausible) if plausible else None


def match_games(normalized_text: str, price_map: dict[str, Any]) -> list[dict[str, object]]:
    matches: list[dict[str, object]] = []
    seen_names: set[str] = set()
    games = price_map.get("games", [])
    if not isinstance(games, list):
        games = []
    for game in games:
        if not isinstance(game, dict):
            continue
        name = str(game.get("name") or "")
        if not name or name in seen_names:
            continue
        aliases = game.get("aliases", [])
        if not isinstance(aliases, list):
            aliases = []
        if any(contains_alias(normalized_text, str(alias)) for alias in [name, *aliases]):
            seen_names.add(name)
            matched: dict[str, object] = {"name": name, "price": int(game.get("price") or 0)}
            for key in ("price_confidence", "price_observation_count", "observation_count"):
                if key in game:
                    matched[key] = game[key]
            matches.append(matched)
    if matches:
        return matches
    crm_candidates: list[dict[str, object]] = []
    for game in load_crm_game_matches():
        if not isinstance(game, dict):
            continue
        name = str(game.get("name") or "")
        if not name or name in seen_names:
            continue
        aliases = game.get("aliases", [])
        if not isinstance(aliases, list):
            aliases = []
        if any(contains_alias(normalized_text, str(alias)) for alias in [name, *aliases]):
            matched = {"name": name, "price": int(game.get("price") or 0)}
            for key in ("price_confidence", "price_observation_count", "observation_count"):
                if key in game:
                    matched[key] = game[key]
            crm_candidates.append(matched)
    crm_candidates.sort(key=lambda item: len(normalize_text(str(item.get("name") or ""))), reverse=True)
    selected_keys: list[str] = []
    for candidate in crm_candidates:
        name = str(candidate.get("name") or "")
        key = normalize_text(name)
        if any(crm_game_name_is_contained(key, selected) for selected in selected_keys):
            continue
        selected_keys.append(key)
        seen_names.add(name)
        matches.append(candidate)
    return matches


def crm_game_name_is_contained(candidate_key: str, selected_key: str) -> bool:
    if not candidate_key or not selected_key:
        return False
    return candidate_key != selected_key and f" {candidate_key} " in f" {selected_key} "


def load_crm_game_matches() -> list[dict[str, object]]:
    global _CRM_GAME_CACHE
    if _CRM_GAME_CACHE is not None:
        return _CRM_GAME_CACHE
    try:
        from .catalog_search import load_catalog

        payload = load_catalog()
    except Exception:
        _CRM_GAME_CACHE = []
        return _CRM_GAME_CACHE
    items = payload.get("items", []) if isinstance(payload, dict) else []
    result: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("canonical_name") or "")
        if not name:
            continue
        prices = item.get("prices") if isinstance(item.get("prices"), dict) else {}
        price = (
            prices.get("friend_resale_price_rub")
            or prices.get("shop_median_rub")
            or prices.get("shop_min_rub")
            or 0
        )
        try:
            price_int = int(price or 0)
        except (TypeError, ValueError):
            price_int = 0
        source = item.get("source") if isinstance(item.get("source"), dict) else {}
        match = {
            "name": name,
            "price": price_int,
            "aliases": item.get("aliases", []),
            "price_confidence": item.get("confidence") or ("low" if not price_int else "medium"),
            "price_observation_count": source.get("source_listing_count") or source.get("db_match_count") or 0,
        }
        result.append(match)
    _CRM_GAME_CACHE = result
    return _CRM_GAME_CACHE


def explicit_disc_count(normalized_text: str) -> int | None:
    english_patterns = [
        r"\b(\d{1,2})\s*(?:game|games|disc|discs|disk|disks)\b",
        r"\b(?:game|games|disc|discs|disk|disks)\s*(\d{1,2})\b",
    ]
    patterns = [
        r"\b(\d{1,2})\s*(?:диск|диска|дисков|игр|игры)\b",
        r"\b(?:диск|диска|дисков|игр|игры)\s*(\d{1,2})\b",
    ]
    for pattern in [*english_patterns, *patterns]:
        match = re.search(pattern, normalized_text)
        if match:
            return int(match.group(1))
    return None


def mentions_physical_games(normalized_text: str) -> bool:
    return any(
        token in normalized_text
        for token in (
            "диск",
            "диски",
            "диска",
            "дисков",
            "игры",
            "игр",
        )
    )


def unknown_disc_count_from(explicit_count: int | None, matched_count: int, has_disc_mention: bool) -> int | str | None:
    if explicit_count is not None:
        return max(0, explicit_count - matched_count)
    if has_disc_mention and matched_count == 0:
        return "unknown"
    return 0


def manual_review_reason(unknown_disc_count: int | str | None) -> str | None:
    if isinstance(unknown_disc_count, int) and unknown_disc_count > 0:
        return f"Указано дисков без распознанных названий: {unknown_disc_count}. Надо посмотреть что за игры."
    if unknown_disc_count == "unknown":
        return "Есть упоминание дисков, но не понятно количество и названия. Надо посмотреть сколько игр и какие они."
    return None


def looks_like_console(normalized_text: str) -> bool:
    return any(
        token in normalized_text
        for token in (
            "ps 4 slim",
            "ps 4 pro",
            "ps 4 fat",
            "ps 4 phat",
            "ps4 slim",
            "ps4 pro",
            "ps4 fat",
            "ps4 phat",
            "playstation 4 slim",
            "playstation 4 pro",
            "playstation 4 fat",
            "playstation 4 phat",
            "игровая приставка",
            "приставка playstation",
            "приставка ps4",
            "консоль playstation",
            "консоль ps4",
            "500gb",
            "500 гб",
            "1tb",
            "1 тб",
        )
    )


def looks_like_console_bundle(normalized_text: str) -> bool:
    has_console = looks_like_console(normalized_text)
    has_bundle = any(token in normalized_text for token in ("игр", "игры", "диск", "диски", "джойстик", "геймпад"))
    return has_console and has_bundle and not normalized_text.startswith("диск ")


def choose_scenario(matches: list[dict[str, object]], disc_count: int, has_console: bool) -> str:
    if has_console and (matches or disc_count):
        return "console_bundle"
    if has_console:
        return "console_only"
    if len(matches) <= 1 and disc_count <= 1:
        return "single_game"
    if matches or disc_count > 1:
        return "game_bundle"
    return "unknown"


def is_excluded_listing(normalized_text: str) -> bool:
    return any(
        token in normalized_text
        for token in (
            "аренд",
            "прокат",
            "скупка",
            "выкуп",
            "деньги сразу",
            "самовывоз приставок",
            "ремонт пристав",
            "сервис пристав",
        )
    )


def estimate_controller_count(normalized_text: str) -> int:
    match = re.search(r"\b(\d)\s*(?:джойстик|джойстика|геймпад|геймпада)\b", normalized_text)
    if match:
        return int(match.group(1))
    if "два джойст" in normalized_text or "2 джойст" in normalized_text or "два геймпад" in normalized_text:
        return 2
    return 1


def console_model(normalized_text: str) -> str:
    if "pro" in normalized_text or "про" in normalized_text:
        return "pro"
    if "slim" in normalized_text:
        return "slim"
    return "fat"
