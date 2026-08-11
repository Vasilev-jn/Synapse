from app.llm_analyzer import ensure_title_console_items
from app.monitor import NewListingRecord


def test_title_console_backfill_adds_ps5_slim_digital_when_llm_misses_main_console() -> None:
    record = NewListingRecord(
        saved_at="2026-08-10T23:30:00",
        fingerprint="x",
        title="Sony playstation 5 slim digital edition 825gb",
        price=38000,
        address="Воронеж",
        description="В комплекте второй джойстик и станция для зарядки",
        url="https://www.avito.ru/item",
        raw_card_texts=[],
        raw_detail_texts=[],
    )
    llm_items = [
        {"name": "DualSense", "canonical_name": "DualSense", "item_type": "controller", "quantity": 2}
    ]

    items = ensure_title_console_items(record, llm_items, original_description=record.description)

    assert items[0]["canonical_name"] == "PlayStation 5 Slim Digital"
    assert items[0]["catalog_entry_id"] == 67
    assert items[0]["item_type"] == "console"
    assert items[1]["name"] == "DualSense"
    assert getattr(record, "llm_title_console_backfilled") is True


def test_title_console_backfill_enriches_existing_generic_console() -> None:
    record = NewListingRecord(
        saved_at="2026-08-10T23:30:00",
        fingerprint="x",
        title="Sony playstation 5 slim digital edition 825gb",
        price=38000,
        address="Воронеж",
        description="В комплекте второй джойстик",
        url="https://www.avito.ru/item",
        raw_card_texts=[],
        raw_detail_texts=[],
    )
    llm_items = [
        {"name": "PlayStation 5 Slim Digital console", "item_type": "console", "quantity": 1}
    ]

    items = ensure_title_console_items(record, llm_items, original_description=record.description)

    assert len(items) == 1
    assert items[0]["canonical_name"] == "PlayStation 5 Slim Digital"
    assert items[0]["catalog_entry_id"] == 67


def test_title_console_backfill_adds_ps4_500gb_cuh_when_llm_failed() -> None:
    record = NewListingRecord(
        saved_at="2026-08-11T00:00:00",
        fingerprint="x",
        title="Игровые приставки Sony Playstation 4 500GB (CUH-1208A)",
        price=14990,
        address="Белгород",
        description="В комплекте: Объем памяти: 500, Количество джойстиков: 1, СЗУ, кабель",
        url="https://www.avito.ru/belgorod/igry_pristavki_i_programmy/igrovye_pristavki_sony_playstation_4_500gb_cuh-1208a_8328950577",
        raw_card_texts=[],
        raw_detail_texts=[],
    )

    items = ensure_title_console_items(record, [], original_description=record.description)

    assert len(items) == 1
    assert items[0]["canonical_name"] == "PlayStation 4 Fat 500GB"
    assert items[0]["catalog_entry_id"] == 59
    assert items[0]["item_type"] == "console"


def test_title_console_backfill_does_not_add_console_for_ps4_disc_listing() -> None:
    record = NewListingRecord(
        saved_at="2026-08-11T12:08:00",
        fingerprint="x",
        title="Диск на ps4 far cry 4",
        price=1500,
        address="Братск",
        description="Обмен на Far Cry 3. Диск приобретён в июле 2026 года.",
        url="https://www.avito.ru/item",
        raw_card_texts=[],
        raw_detail_texts=[],
    )
    llm_items = [
        {"name": "Far Cry 4", "canonical_name": "Far Cry 4", "item_type": "game", "platform": "PS4", "quantity": 1}
    ]

    items = ensure_title_console_items(record, llm_items, original_description=record.description)

    assert len(items) == 1
    assert items[0]["canonical_name"] == "Far Cry 4"
    assert all(item.get("item_type") != "console" for item in items)
    assert not getattr(record, "llm_title_console_backfilled", False)


def test_title_console_backfill_does_not_add_console_for_ps5_game_listing() -> None:
    record = NewListingRecord(
        saved_at="2026-08-11T12:07:00",
        fingerprint="x",
        title="Diablo 4 ps5",
        price=2850,
        address="Казань",
        description="Диск в хорошем состоянии",
        url="https://www.avito.ru/item",
        raw_card_texts=[],
        raw_detail_texts=[],
    )
    llm_items = [
        {"name": "Diablo 4", "canonical_name": "Diablo 4", "item_type": "game", "platform": "PS5", "quantity": 1}
    ]

    items = ensure_title_console_items(record, llm_items, original_description=record.description)

    assert len(items) == 1
    assert items[0]["canonical_name"] == "Diablo 4"
    assert all(item.get("item_type") != "console" for item in items)
    assert not getattr(record, "llm_title_console_backfilled", False)
