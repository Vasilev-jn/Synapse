from __future__ import annotations

from app.monitor import NewListingRecord
from app.notifier import format_listing_plain_message, format_profit_evaluation_message


def test_listing_message_hides_all_regions_and_raw_url() -> None:
    record = NewListingRecord(
        saved_at="2026-08-10T10:00:00",
        fingerprint="x",
        title="Sony PS4 Slim",
        price=15000,
        address="Во всех регионах",
        description="Комплект в хорошем состоянии",
        url="https://www.avito.ru/item",
        raw_card_texts=[],
        raw_detail_texts=[],
        delivery_price_rub=199,
    )
    setattr(record, "posted_at_text", "сегодня в 22:30")

    setattr(record, "bot_listing_id", "8328950577")
    text = format_listing_plain_message(record)

    assert text.startswith("<b>#8328950577</b>")
    assert "Цена: <b>15000 ₽</b>" in text
    assert "Дата публикации: сегодня в 22:30" in text
    assert "Адрес: не нашёл" in text
    assert "Доставка: от 199 ₽" in text
    assert '<a href="https://www.avito.ru/item">Открыть объявление</a>' in text
    assert "\nhttps://www.avito.ru/item" not in text


def test_analysis_message_is_compact() -> None:
    evaluation = {
        "bot_listing_id": "8328950577",
        "total": {
            "buy_total": 15000,
            "expected_sell_total": 18250,
            "expected_net_total": 15878,
            "expected_profit": 878,
        },
        "items": [
            {"canonical_name": "PlayStation 4 Slim 500GB", "buy_price_used": 3750, "expected_sell_price": 17000},
            {"name": "Unknown PS4 game", "buy_price_used": 3750, "expected_sell_price": None},
            {"canonical_name": "DualShock 4", "buy_price_used": 3750, "expected_sell_price": 1250},
        ],
    }

    text = format_profit_evaluation_message(evaluation)
    assert text.startswith("<b>#8328950577</b>")

    assert "Цена покупки: <b>15000 ₽</b>" in text
    assert "PlayStation 4 Slim 500GB — 3750 ₽" in text
    assert "Unknown PS4 game — 3750 ₽" in text
    assert "Цена продажи: <b>18250 ₽</b>" in text
    assert "Профит: <b>+878 ₽</b>" in text
    assert "Расчёт:" not in text
    assert "Риски" not in text
    assert "Решение" not in text


def test_analysis_message_collapses_large_lots() -> None:
    evaluation = {
        "total": {
            "buy_total": 10000,
            "expected_sell_total": 30000,
            "expected_profit": 15000,
        },
        "items": [
            {"canonical_name": f"Game {index}", "expected_sell_price": 1000}
            for index in range(21)
        ],
    }

    text = format_profit_evaluation_message(evaluation)

    assert "Товаров слишком много: 21" in text
    assert "Game 0 —" not in text


def test_analysis_message_marks_unpriced_items_as_unknown() -> None:
    evaluation = {
        "total": {
            "buy_total": 15000,
            "expected_sell_total": 19100,
            "expected_net_total": 16618,
            "expected_profit": 1618,
        },
        "items": [
            {"canonical_name": "PlayStation 4 Slim 1TB", "buy_price_used": 14018},
            {"name": "Unknown PS4 game", "buy_price_used": None},
        ],
        "risks": ["profit_excludes_unpriced_items"],
    }

    text = format_profit_evaluation_message(evaluation)

    assert "PlayStation 4 Slim 1TB — 14018 ₽" in text
    assert "Unknown PS4 game — непонятно" in text
    assert "Профит не учитывает позиции «непонятно»." in text


def test_analysis_message_does_not_show_fake_negative_profit_when_nothing_is_priced() -> None:
    evaluation = {
        "total": {
            "buy_total": 1100,
            "expected_sell_total": 0,
            "expected_profit": -1100,
        },
        "items": [
            {"name": "Uncharted collection ps4", "buy_price_used": None, "expected_sell_price": None},
        ],
        "risks": ["profit_excludes_unpriced_items"],
    }

    text = format_profit_evaluation_message(evaluation)

    assert "-1100" not in text
    assert "Uncharted collection ps4" in text
