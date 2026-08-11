from __future__ import annotations

from dataclasses import dataclass

from app.main import (
    compact_external_result,
    description_summary_threshold,
    link_seen_key,
    listing_reservation_status,
    normalized_listing_text,
    telegram_suppression_reason,
    truncate_description_for_telegram,
)


@dataclass
class Record:
    title: str | None = None
    description: str | None = None
    address: str | None = None
    delivery_text: str | None = None
    raw_card_texts: list[str] | None = None
    raw_detail_texts: list[str] | None = None


def test_telegram_suppression_filters_rental_subscription_and_digital():
    assert telegram_suppression_reason(Record(title="PS4 в аренду на сутки")) == "rental_or_prokat"
    assert telegram_suppression_reason(Record(title="PS Plus Extra подписка")) == "subscription"
    assert telegram_suppression_reason(Record(title="Аккаунт PS4 с играми")) == "digital_or_account"


def test_subscription_is_not_suppressed_when_listing_looks_like_hardware():
    record = Record(title="PS4 Slim 1TB + подписка PS Plus")
    assert telegram_suppression_reason(record) is None


def test_reservation_and_normalized_text():
    record = Record(title="Ёмкая приставка", description="Товар забронирован на сегодня")
    assert "емкая приставка" in normalized_listing_text(record)
    assert listing_reservation_status(record) == "reserved"


def test_payload_helpers_are_stable():
    assert link_seen_key(None, "https://example.test/a") == "<no-fingerprint>|https://example.test/a"
    assert description_summary_threshold({"pipeline": {"description_summary_threshold_chars": 42}}) == 42
    assert truncate_description_for_telegram("а " * 400, threshold=20).endswith("...")
    assert compact_external_result({"ok": True, "skipped": False, "status": 200, "ignored": 1}) == {
        "ok": True,
        "skipped": False,
        "status": 200,
    }
