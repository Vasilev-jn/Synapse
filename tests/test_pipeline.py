from app.pipeline import item_id_from_url, monitor_json_to_record
from app.llm_analyzer import (
    extract_price_list_lines_from_record,
    parse_json_items_with_status,
    price_line_chunk_items_to_listing_items,
    should_use_heavy_llm,
)
from app.monitor import NewListingRecord


def test_monitor_json_to_record_normalizes_collected_card():
    record = monitor_json_to_record(
        {
            "canonical_url": "https://www.avito.ru/moskva/igry/igra_ps4_1234567890",
            "collected_at": "2026-08-08T20:00:00",
            "visible": {
                "title": "Игра PS4 Bloodborne",
                "price": "1 500 ₽",
                "description": "Диск в хорошем состоянии",
                "address": "Москва, Арбат",
                "delivery": "Доставка от 199 ₽",
            },
        }
    )

    assert item_id_from_url(record.url) == "1234567890"
    assert record.title == "Игра PS4 Bloodborne"
    assert record.price == 1500
    assert record.delivery_price_rub == 199
    assert record.delivery_status == "delivery_available_price_found"
    assert record.seller_city == "Москва"
    assert record.item_kind == "game"


def test_monitor_json_to_record_takes_concrete_address_from_html(tmp_path):
    html_path = tmp_path / "item.html"
    html_path.write_text(
        '<div id="item-view-address"><div itemprop="address">'
        "<p>Саранск</p><span>Узнать подробности</span><ymaps></ymaps>"
        "</div></div>",
        encoding="utf-8",
    )

    record = monitor_json_to_record(
        {
            "canonical_url": "https://www.avito.ru/saransk/igry/ps4_1234567890",
            "collected_at": "2026-08-08T20:00:00",
            "visible": {
                "title": "PS4",
                "price": "15 000 ₽",
                "address": "Во всех регионах",
                "delivery": "Купить с доставкой",
            },
            "snapshot": {"html": str(html_path)},
        }
    )

    assert record.address == "Саранск"
    assert getattr(record, "listing_field_statuses")["address"] == "Саранск"
    assert getattr(record, "listing_field_statuses")["delivery"] == "есть, цена не найдена"


def test_heavy_llm_routing_detects_big_price_list():
    record = NewListingRecord(
        saved_at="2026-08-08T20:00:00",
        fingerprint="lot",
        title="Большой список игр PS4",
        price=1000,
        address=None,
        description="\n".join(
            [
                "Много игр, цены ниже, список:",
                "GTA V - 1200 ₽",
                "FIFA 20 - 600 ₽",
                "Hogwarts Legacy - 1200 ₽",
                "God of War - 1000 ₽",
                "Bloodborne - 1000 ₽",
                "Uncharted 4 - 950 ₽",
            ]
        ),
        url="https://www.avito.ru/moskva/igry/lot_1234567890",
        raw_card_texts=[],
        raw_detail_texts=[],
    )

    assert should_use_heavy_llm(record)


def test_empty_items_json_does_not_need_fallback():
    items, parsed_items_key = parse_json_items_with_status('{"items":[]}')

    assert items == []
    assert parsed_items_key is True


def test_price_line_parser_extracts_product_prices():
    record = NewListingRecord(
        saved_at="2026-08-08T20:00:00",
        fingerprint="lot",
        title="PS4 games",
        price=1000,
        address=None,
        description="GTA V - 1200 ₽\nGod of War - 1000 ₽\nA Way Out - нету",
        url="https://www.avito.ru/moskva/igry/lot_1234567890",
        raw_card_texts=[],
        raw_detail_texts=[],
    )

    rows = extract_price_list_lines_from_record(record)

    assert [(row["name"], row["price_rub"]) for row in rows] == [
        ("GTA V", 1200),
        ("God of War", 1000),
    ]


def test_price_line_chunk_items_convert_to_market_priced_listing_items():
    items = price_line_chunk_items_to_listing_items(
        [
            {
                "line_no": 1,
                "original_name": "GTA V",
                "normalized_name": "Grand Theft Auto V",
                "price_rub": 1200,
                "platform": "PS4",
                "item_type": "game",
                "confidence": "high",
            }
        ],
        [{"line_no": 1, "name": "GTA V", "price_rub": 1200, "source_line": "GTA V - 1200 ₽"}],
    )

    assert items[0]["name"] == "Grand Theft Auto V"
    assert items[0]["price_rub"] == 1200
    assert items[0]["price_source_type"] == "description_item_price"
    assert items[0]["use_for_market_pricing"] is True
