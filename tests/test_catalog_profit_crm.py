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
