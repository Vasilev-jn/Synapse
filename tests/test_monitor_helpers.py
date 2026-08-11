from app.monitor import (
    delivery_price_from_text,
    delivery_status_from_text,
    delivery_text_from_texts,
    item_kind_from_title,
    seller_city_from_address,
)


def test_delivery_helpers_extract_avito_delivery_price():
    text = delivery_text_from_texts(["Описание", "Доставка от 249 ₽", "Написать продавцу"])
    assert text == "Доставка от 249 ₽"
    assert delivery_price_from_text(text) == 249
    assert delivery_status_from_text(text) == "delivery_available_price_found"


def test_delivery_helpers_ignore_installment_price_near_delivery_block():
    text = (
        "Скидка на доставку до 100% кешбэк от Альфа-Банка "
        "Купить с доставкой В корзину Авито Доставка. "
        "Можно оплатить при получении Об Авито Доставке "
        "2 131 ₽ × 10 месяцев Без первого взноса"
    )
    assert delivery_price_from_text(text) is None
    assert delivery_status_from_text(text) == "delivery_available_price_unknown"


def test_delivery_helpers_detect_absent_delivery():
    assert delivery_status_from_text("Только самовывоз, доставки нет") == "delivery_not_available"


def test_kind_and_city_helpers():
    assert seller_city_from_address("Москва, Тверская 1") == "Москва"
    assert item_kind_from_title("Sony PS4 Pro 1TB") == "console"
    assert item_kind_from_title("Игра Spider-Man для PS4") == "game"
