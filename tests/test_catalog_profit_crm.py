from app import crm_market
from app.catalog_search import catalog_candidates_for_text, normalize_catalog_text
from app.crm_market import avito_external_id, normalize_extracted_items_for_crm
from app.profit_estimator import extract_delivery_price_rub, normalize_text


def test_catalog_search_uses_transferred_crm_catalog():
    assert normalize_catalog_text("Marvel’s Spider-Man PS4") == "marvel s spider man ps 4"
    candidates = catalog_candidates_for_text("Spider Man PS4 диск", limit=5)
    assert isinstance(candidates, list)


def test_profit_and_crm_helpers():
    assert normalize_text("Ёлка PS4") == "елка ps4"
    assert extract_delivery_price_rub("Доставка от 199 ₽") == 199
    assert avito_external_id("https://www.avito.ru/moskva?q=x_1234567890") == "1234567890"
    items = normalize_extracted_items_for_crm([{"name": "Bloodborne", "item_type": "game", "quantity": "2"}])
    assert items[0]["name"] == "Bloodborne"


def test_console_match_uses_storage_parameter_format():
    items = normalize_extracted_items_for_crm(
        [{"name": "PlayStation 4 Slim", "item_type": "console"}],
        listing_text="Характеристики Модель: PlayStation 4 Slim Встроенная память, ГБ: 1000",
    )

    assert items[0]["catalog_entry_id"] == 62
    assert items[0]["canonical_name"] == "PlayStation 4 Slim 1TB"


def test_unsafe_llm_game_match_is_cleared_and_queued(tmp_path, monkeypatch):
    monkeypatch.setattr(crm_market, "ALIAS_REVIEW_QUEUE_PATH", tmp_path / "alias_review.jsonl")

    items = normalize_extracted_items_for_crm(
        [
            {
                "name": "Horizon Zero Dawn",
                "canonical_name": "Horizon Zero Dawn",
                "catalog_item_id": "horizon_zero_dawn",
                "item_type": "game",
            }
        ],
        listing_text="Uncharted collection ps4\nDisc in good condition",
    )

    assert items[0]["name"] == "Uncharted collection ps4"
    assert items[0]["canonical_name"] is None
    assert items[0]["catalog_item_id"] is None
    assert items[0]["missing_data_reason"] == "llm_item_name_not_found_in_listing_text"
    assert (tmp_path / "alias_review.jsonl").read_text(encoding="utf-8").count("\n") == 1


def test_game_disc_listing_cannot_be_priced_as_console(tmp_path, monkeypatch):
    monkeypatch.setattr(crm_market, "ALIAS_REVIEW_QUEUE_PATH", tmp_path / "alias_review.jsonl")

    items = normalize_extracted_items_for_crm(
        [
            {
                "name": "PlayStation 5 Disc",
                "canonical_name": "PlayStation 5 Disc",
                "catalog_entry_id": 64,
                "item_type": "console",
            }
        ],
        listing_text="AD Infinitum PS5 (NEW)\nGame Ad Infinitum for PS5, physical disc",
    )

    assert items[0]["name"] == "AD Infinitum PS5 (NEW)"
    assert items[0]["item_type"] == "game"
    assert items[0]["canonical_name"] is None
    assert items[0]["catalog_entry_id"] is None
    assert items[0]["missing_data_reason"] == "console_match_rejected_for_game_disc_listing"
