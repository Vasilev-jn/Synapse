from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import Settings
from app.deduplication import deduplicate_all
from app.db import get_engine, init_db, make_session_factory
from app.inventory import (
    ACCESSORY_CATEGORY,
    CONSOLE_CATEGORY,
    GAME_CATEGORY,
    ItemInput,
    add_subcategory,
    create_item,
    dashboard_stats,
    ensure_alias,
    ensure_catalog_entry,
    export_items_csv,
    item_profit,
    seed_database,
    sell_item,
)
from app.market import MATCH_AMBIGUOUS, confirm_market_match, import_avito_api_batch, import_market_files, market_estimate
from app.models import AnalysisItem, CatalogAlias, CatalogEntry, Category, Item, MarketImport, MarketListing, MarketListingMatch, PriceObservation, utcnow
from app.price_observations import OBS_OWN_SALE, record_own_sale_observation
from app.avito_evaluator import PriceCandidate, trim_price_candidates, weighted_median


def _game_category(session):
    return session.scalar(select(Category).where(Category.name == GAME_CATEGORY))


def _category(session, name):
    return session.scalar(select(Category).where(Category.name == name))


def test_seed_can_run_twice_without_duplicates(db_session) -> None:
    seed_database(db_session)
    seed_database(db_session)
    db_session.flush()

    assert db_session.scalar(select(func.count(Category.id))) == 4
    assert db_session.scalar(select(func.count(CatalogEntry.id))) == 15


def test_seed_creates_distinct_ps5_console_catalog_entries(db_session) -> None:
    seed_database(db_session)
    names = {row.name for row in db_session.scalars(select(CatalogEntry))}

    assert {
        "PlayStation 5 Disc",
        "PlayStation 5 Digital",
        "PlayStation 5 Slim Disc",
        "PlayStation 5 Slim Digital",
    } <= names


def test_weighted_median_trims_three_percent_tails_for_large_samples() -> None:
    candidates = [
        PriceCandidate(
            price=price,
            weight=1.0,
            canonical_name="Test",
            item_type="game",
            source="test",
            confidence=1.0,
            reason="test",
            price_source="test",
            matched_by="test",
            match_score=1.0,
        )
        for price in ([100] + [1000 + index for index in range(38)] + [100000])
    ]

    trimmed = trim_price_candidates(candidates)

    assert len(trimmed) == 38
    assert min(candidate.price for candidate in trimmed) == 1000
    assert max(candidate.price for candidate in trimmed) == 1037
    assert weighted_median(trimmed) < 100000


def test_system_categories_are_created(db_session) -> None:
    seed_database(db_session)
    names = {row.name for row in db_session.scalars(select(Category))}

    assert {"Игры, приставки и программы", "Игры для приставок", "Игровые приставки", "Аксессуары"} <= names


def test_settings_accept_postgres_database_url() -> None:
    settings = Settings(database_url="postgresql://postgres:postgres@localhost:5432/crm_inventory")

    assert settings.database_url.startswith("postgresql+psycopg://")
    assert not settings.is_sqlite
    assert settings.database_label == settings.database_url


def test_custom_subcategory_is_saved(db_session) -> None:
    seed_database(db_session)
    parent = db_session.scalar(select(Category).where(Category.name == "Аксессуары"))

    child = add_subcategory(db_session, parent_id=parent.id, name="Геймпады")

    assert child.parent_id == parent.id
    assert not child.is_system


def test_catalog_dedup_merges_case_duplicates(db_session) -> None:
    seed_database(db_session)
    games = _game_category(db_session)
    first = ensure_catalog_entry(db_session, category_id=games.id, name="Ghost of Tsushima")
    second = ensure_catalog_entry(db_session, category_id=games.id, name=" ghost  OF tsushima ")

    assert first.id == second.id

    duplicate = CatalogEntry(category_id=games.id, name="GHOST OF TSUSHIMA")
    db_session.add(duplicate)
    db_session.flush()

    stats = deduplicate_all(db_session)

    assert stats.catalog_entries_merged == 1
    entries = list(db_session.scalars(select(CatalogEntry).where(CatalogEntry.category_id == games.id)))
    assert sum(1 for entry in entries if entry.name.casefold().replace(" ", "") == "ghostoftsushima") == 1


def test_separate_manual_items_create_physical_items(db_session) -> None:
    seed_database(db_session)
    games = _game_category(db_session)

    items = [
        create_item(
            db_session,
            item_input=ItemInput(
                category_id=games.id,
                catalog_name="Hogwarts Legacy",
                calculated_cost=3000,
                platform="PlayStation 5",
            ),
        )
        for _index in range(3)
    ]
    db_session.flush()

    assert len(items) == 3
    assert len({item.id for item in items}) == 3
    assert all(item.code and item.code.startswith("ITEM-") for item in items)
    assert all(item.calculated_cost == 3000 for item in items)


def test_item_without_title_is_rejected(db_session) -> None:
    seed_database(db_session)
    games = _game_category(db_session)

    try:
        create_item(db_session, item_input=ItemInput(category_id=games.id, catalog_name=""))
    except ValueError as exc:
        assert "Название товара обязательно" in str(exc)
    else:
        raise AssertionError("item without title was accepted")


def test_two_same_games_are_distinct_items_and_cost_can_be_changed(db_session) -> None:
    seed_database(db_session)
    games = _game_category(db_session)
    first = create_item(db_session, item_input=ItemInput(category_id=games.id, catalog_name="Hogwarts Legacy", calculated_cost=1000))
    second = create_item(db_session, item_input=ItemInput(category_id=games.id, catalog_name="Hogwarts Legacy", calculated_cost=1000))
    first.calculated_cost = 700
    db_session.flush()

    assert first.id != second.id
    assert first.calculated_cost == 700


def test_sale_changes_status_and_profit_is_calculated(db_session) -> None:
    seed_database(db_session)
    games = _game_category(db_session)
    created = create_item(
        db_session,
        item_input=ItemInput(category_id=games.id, catalog_name="Ghost of Tsushima", calculated_cost=1000),
    )

    item = sell_item(db_session, item_id=created.id, sale_price=1500, sold_at=None)
    record_own_sale_observation(db_session, item)
    db_session.flush()
    observation = db_session.scalar(select(PriceObservation).where(PriceObservation.item_id == item.id))

    assert item.status == "Продан"
    assert item_profit(item) == 500
    assert observation is not None
    assert observation.observation_type == OBS_OWN_SALE
    assert observation.price == 1500
    assert observation.confidence == 1.0


def test_profit_is_unknown_without_cost(db_session) -> None:
    seed_database(db_session)
    games = _game_category(db_session)
    item = create_item(
        db_session,
        item_input=ItemInput(category_id=games.id, catalog_name="New Game", calculated_cost=None),
    )
    item.calculated_cost = None
    item.sale_price = 1000

    assert item_profit(item) is None


def test_user_can_add_own_catalog_entry_and_alias_limit_is_enforced(db_session) -> None:
    seed_database(db_session)
    games = _game_category(db_session)
    entry = ensure_catalog_entry(db_session, category_id=games.id, name="My Own Game")
    ensure_alias(db_session, entry, "Моя игра")
    ensure_alias(db_session, entry, "My Game")

    try:
        ensure_alias(db_session, entry, "Third alias")
    except ValueError:
        pass
    else:
        raise AssertionError("third alias was accepted")

    assert db_session.scalar(select(func.count(CatalogAlias.id)).where(CatalogAlias.catalog_entry_id == entry.id)) == 2


def test_market_import_deduplicates_and_updates_last_seen(db_session, tmp_path) -> None:
    seed_database(db_session)
    file_path = tmp_path / "market.json"
    file_path.write_text(
        json.dumps(
            [
                {"id": "m1", "title": "Ghost of Tsushima PS4 диск", "price": 1000, "userType": "private"},
                {"id": "m1", "title": "Ghost of Tsushima PS4 диск", "price": 1100, "userType": "private"},
            ]
        ),
        encoding="utf-8",
    )

    stats = import_market_files(db_session, [str(file_path)])
    listing = db_session.scalar(select(MarketListing).where(MarketListing.external_id == "m1"))

    assert stats.raw_count == 2
    assert stats.duplicate_count == 1
    assert listing.price == 1100


def test_market_estimate_excludes_digital_subscription_and_ambiguous(db_session, tmp_path) -> None:
    seed_database(db_session)
    games = _game_category(db_session)
    entry = db_session.scalar(select(CatalogEntry).where(CatalogEntry.name == "Ghost of Tsushima"))
    file_path = tmp_path / "market.json"
    file_path.write_text(
        json.dumps(
            [
                {"id": "p1", "title": "Ghost of Tsushima PS4 диск", "price": 1000, "userType": "private"},
                {"id": "p2", "title": "Ghost of Tsushima PS4 диск", "price": 1500, "userType": "private"},
                {"id": "p3", "title": "Ghost of Tsushima PS4 диск", "price": 2000, "userType": "private"},
                {"id": "d1", "title": "Ghost of Tsushima PS4 цифровой аккаунт", "price": 100, "userType": "private"},
                {"id": "s1", "title": "PS Plus подписка Ghost of Tsushima", "price": 50, "userType": "private"},
            ]
        ),
        encoding="utf-8",
    )
    import_market_files(db_session, [str(file_path)])
    ambiguous = db_session.scalar(select(MarketListing).where(MarketListing.external_id == "p3"))
    confirm_market_match(db_session, listing_id=ambiguous.id, catalog_entry_id=entry.id, status=MATCH_AMBIGUOUS)
    db_session.flush()

    estimate = market_estimate(db_session, catalog_entry_id=entry.id, platform="PlayStation 4")

    assert estimate.count == 2
    assert estimate.median_price == 1250


def test_csv_export_is_created(db_session, tmp_path, monkeypatch) -> None:
    seed_database(db_session)
    games = _game_category(db_session)
    create_item(
        db_session,
        item_input=ItemInput(category_id=games.id, catalog_name="Ghost of Tsushima", calculated_cost=1000),
    )
    db_session.flush()

    path = export_items_csv(db_session, tmp_path / "items.csv")

    assert path.exists()
    assert "ITEM-" in path.read_text(encoding="utf-8-sig")


def test_zero_cost_is_shown_as_free(db_session, tmp_path) -> None:
    seed_database(db_session)
    games = _game_category(db_session)
    item = create_item(
        db_session,
        item_input=ItemInput(category_id=games.id, catalog_name="Free Game", calculated_cost=0),
    )
    db_session.flush()

    assert item.calculated_cost == 0
    path = export_items_csv(db_session, tmp_path / "free_items.csv")

    assert "Бесплатно" in path.read_text(encoding="utf-8-sig")


def test_main_pages_respond_without_errors(tmp_path) -> None:
    from app.main import app, get_db

    database_url = f"sqlite:///{(tmp_path / 'web.sqlite').as_posix()}"
    engine = get_engine(database_url=database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        seed_database(session)
        session.commit()

    def override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    for path in ["/", "/bought", "/sold", "/stats", "/history", "/bought/new-disc", "/items", "/catalog", "/categories", "/market"]:
        response = client.get(path)
        assert response.status_code in (200, 303), path
    exchange_redirect = client.get("/exchange", follow_redirects=False)
    assert exchange_redirect.status_code == 303
    assert exchange_redirect.headers["location"] == "/history"
    add_page = client.get("/bought/new-disc")
    assert "PlayStation 4" in add_page.text
    assert "PlayStation 5" in add_page.text
    assert "PlayStation 4 Fat" not in add_page.text
    assert "Red Dead Redemption 2" in add_page.text
    assert 'data-category="console"' in add_page.text
    assert 'data-category="accessory"' in add_page.text
    assert "DualShock 4" in add_page.text
    assert "Кабель HDMI" in add_page.text
    assert 'data-show-default="true"' in add_page.text
    assert 'data-default-suggestion="true"' in add_page.text
    app.dependency_overrides.clear()


def test_avito_api_batch_deduplicates_without_creating_matches(db_session) -> None:
    result = import_avito_api_batch(
        db_session,
        source="adb_bot",
        filename="adb_bot_live",
        scraped_at=None,
        listings=[
            {
                "external_id": "8261155969",
                "title": "Sony Playstation 4 Slim 1Tb",
                "description": "console",
                "url": "https://www.avito.ru/item/8261155969",
                "price": 16000,
                "currency": None,
                "address": "Ust-Ilimsk",
                "platform": "PS4",
                "format": "physical",
                "type": "console_bundle",
                "raw_json": {
                    "source": "adb_bot",
                    "fingerprint": "ps4-slim-1tb|16000|ust-ilimsk",
                    "llm_market_price_observations": [
                        {
                            "name": "Sony Playstation 4 Slim 1Tb",
                            "item_type": "console",
                            "platform": "PS4",
                            "price_rub": 16000,
                            "price_confidence": "medium",
                            "price_scope": "per_item",
                            "price_source_type": "listing_price_single_item",
                        }
                    ],
                },
            },
            {
                "external_id": "8261155969",
                "title": "Sony Playstation 4 Slim 1Tb",
                "price": 15500,
                "raw_json": {
                    "source": "adb_bot",
                    "fingerprint": "ps4-slim-1tb|16000|ust-ilimsk",
                    "llm_market_price_observations": [
                        {
                            "name": "Sony Playstation 4 Slim 1Tb",
                            "item_type": "console",
                            "platform": "PS4",
                            "price_rub": 15500,
                            "price_confidence": "medium",
                            "price_scope": "per_item",
                            "price_source_type": "listing_price_single_item",
                        }
                    ],
                },
            },
        ],
    )
    db_session.flush()

    listing = db_session.get(MarketListing, result.created_listing_ids[0])

    assert result.raw_count == 1
    assert result.unique_count == 1
    assert result.duplicate_count == 1
    assert result.duplicate_listing_ids == [listing.id]
    assert listing.price == 15500
    assert listing.currency == "RUB"
    assert db_session.scalar(select(func.count(MarketImport.id))) == 1
    import_row = db_session.scalar(select(MarketImport))
    assert import_row.raw_count == 1
    assert import_row.unique_count == 1
    assert import_row.duplicate_count == 0
    assert db_session.scalar(select(func.count(MarketListing.id))) == 1
    assert db_session.scalar(select(func.count(MarketListingMatch.id))) == 0
    observation = db_session.scalar(select(PriceObservation).where(PriceObservation.market_listing_id == listing.id))
    assert observation is not None
    assert observation.observation_type == "avito_market"
    assert observation.price == 15500
    assert observation.usable_for_auto_price is True


def test_avito_market_import_endpoint_and_token(tmp_path) -> None:
    from app.main import app, get_db, settings

    database_url = f"sqlite:///{(tmp_path / 'api_market.sqlite').as_posix()}"
    engine = get_engine(database_url=database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)

    def override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    payload = {
        "source": "adb_bot",
        "filename": "adb_bot_live",
        "scraped_at": "2026-07-27T02:30:00+03:00",
        "crm_sent_at": "2026-07-27T02:30:05+03:00",
        "listings": [
            {
                "external_id": "8261155969",
                "title": "Sony Playstation 4 Slim 1Tb",
                "description": "Console PlayStation 4 Slim",
                "url": "https://www.avito.ru/item/8261155969",
                "price": 16000,
                "currency": "RUB",
                "address": "Ust-Ilimsk",
                "platform": "PS4",
                "format": "physical",
                "type": "console_bundle",
                "localization": None,
                "seller_name": None,
                "seller_user_key": None,
                "seller_type": None,
                "is_shop": False,
                "posted_at": None,
                "scraped_at": "2026-07-27T02:28:00+03:00",
                "raw_json": {
                    "source": "adb_bot",
                    "bot_saved_at": "2026-07-27T02:28:00+03:00",
                    "crm_sent_at": "2026-07-27T02:30:05+03:00",
                    "delivery_text": "Доставка от 1 дня, от 89 ₽",
                    "delivery_price_rub": 89,
                    "delivery_status": "delivery_not_found",
                    "delivery_price": None,
                    "reserved": False,
                    "fingerprint": "playstation-4-slim-1tb|16000|ust-ilimsk",
                    "llm_market_price_observations": [
                        {
                            "name": "Sony Playstation 4 Slim 1Tb",
                            "item_type": "console",
                            "platform": "PS4",
                            "price_rub": 16000,
                            "price_confidence": "medium",
                            "price_scope": "per_item",
                            "price_source_type": "listing_price_single_item",
                        }
                    ],
                },
            }
        ],
    }

    app.dependency_overrides[get_db] = override_db
    old_token = settings.market_import_token
    settings.market_import_token = "secret"
    try:
        client = TestClient(app)
        assert client.post("/api/market/imports/avito", json=payload).status_code == 401

        response = client.post(
            "/api/market/imports/avito",
            json=payload,
            headers={"Authorization": "Bearer secret"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["raw_count"] == 1
        assert data["unique_count"] == 1
        assert data["duplicate_count"] == 0
        assert len(data["created_listing_ids"]) == 1

        with session_factory() as session:
            import_row = session.scalar(select(MarketImport).where(MarketImport.id == data["import_id"]))
            listing_row = session.scalar(select(MarketListing).where(MarketListing.id == data["created_listing_ids"][0]))
            assert import_row is not None
            assert listing_row is not None
            assert import_row.scraped_at.isoformat() == "2026-07-26T23:30:00"
            assert import_row.crm_sent_at.isoformat() == "2026-07-26T23:30:05"
            assert listing_row.scraped_at.isoformat() == "2026-07-26T23:28:00"
            assert listing_row.raw_json["bot_saved_at"] == "2026-07-27T02:28:00+03:00"
            assert listing_row.raw_json["delivery_text"] == "Доставка от 1 дня, от 89 ₽"
            assert listing_row.raw_json["delivery_price_rub"] == 89
            assert listing_row.raw_json["delivery_status"] == "delivery_not_found"
            observation = session.scalar(select(PriceObservation).where(PriceObservation.market_listing_id == listing_row.id))
            assert observation is not None
            assert observation.price == 16000
            assert observation.delivery_price == 89
            assert observation.price_with_delivery == 16089
            assert observation.usable_for_auto_price is True

        duplicate_response = client.post(
            "/api/market/imports/avito",
            json=payload,
            headers={"Authorization": "Bearer secret"},
        )
        assert duplicate_response.status_code == 200
        duplicate_data = duplicate_response.json()
        assert duplicate_data["import_id"] is None
        assert duplicate_data["raw_count"] == 0
        assert duplicate_data["unique_count"] == 0
        assert duplicate_data["duplicate_count"] == 1

        history_response = client.get("/history")
        assert history_response.status_code == 200
        assert "Бот API" in history_response.text
        assert "Передано объявлений" in history_response.text
        assert "Дублей:" in history_response.text
        assert "Sony Playstation 4 Slim 1Tb" in history_response.text

        summary_response = client.get("/api/market/imports/avito/summary")
        assert summary_response.status_code == 200
        summary = summary_response.json()
        assert summary["raw_count"] == 1
        assert summary["unique_count"] == 1
        assert summary["duplicate_count"] == 0
        assert summary["last_listing_saved_label"] == "27.07.2026 02:28"
        assert summary["last_crm_sent_label"] == "27.07.2026 02:30"
        assert summary["recent"][0]["raw_count"] == 1
        assert summary["recent"][0]["duplicate_count"] == 0
        assert summary["recent"][0]["crm_sent_at_label"] == "27.07.2026 02:30"
        assert summary["recent_listings"][0]["title"] == "Sony Playstation 4 Slim 1Tb"
        assert summary["recent_listings"][0]["sent_at_label"] == "27.07.2026 02:30"
    finally:
        settings.market_import_token = old_token
        app.dependency_overrides.clear()


def test_avito_listing_evaluator_uses_crm_observations(tmp_path) -> None:
    from app.main import app, get_db, settings

    database_url = f"sqlite:///{(tmp_path / 'evaluate.sqlite').as_posix()}"
    engine = get_engine(database_url=database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        seed_database(session)
        games = _game_category(session)
        item = create_item(
            session,
            item_input=ItemInput(category_id=games.id, catalog_name="Ghost of Tsushima", calculated_cost=1000),
        )
        item = sell_item(session, item_id=item.id, sale_price=1700, sold_at=None)
        record_own_sale_observation(session, item)
        session.commit()

    def override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    old_token = settings.market_import_token
    settings.market_import_token = None
    try:
        client = TestClient(app)
        response = client.post(
            "/api/market/evaluate-avito-listing",
            json={
                "listing": {"price": 1000, "raw_json": {"delivery_price_rub": 100}},
                "extracted_items": [{"name": "Ghost of Tsushima", "item_type": "game", "quantity": 1}],
            },
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json; charset=utf-8"
        assert "Нужно проверить" in response.content.decode("utf-8")
        data = response.json()
        assert data["ok"] is True
        assert data["decision"] == "check"
        assert data["total"]["buy_total"] == 1100
        assert data["total"]["expected_profit"] > 300
        assert data["items"][0]["expected_sell_price"] == 1700
        assert data["items"][0]["confidence"] in {"medium", "high"}
    finally:
        settings.market_import_token = old_token
        app.dependency_overrides.clear()


def test_avito_listing_evaluator_marks_fallback_as_low_confidence_and_blocks_good(tmp_path) -> None:
    from app.main import app, get_db, settings

    database_url = f"sqlite:///{(tmp_path / 'evaluate-fallback.sqlite').as_posix()}"
    engine = get_engine(database_url=database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        seed_database(session)
        session.commit()

    def override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    old_token = settings.market_import_token
    settings.market_import_token = None
    try:
        client = TestClient(app)
        response = client.post(
            "/api/market/evaluate-avito-listing",
            json={
                "listing": {"price": 100},
                "extracted_items": [{"name": "Minecraft", "item_type": "game", "quantity": 1}],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["decision"] == "check"
        assert data["fallback_only"] is True
        assert data["items"][0]["source"] == "fallback"
        assert "fallback / низкая уверенность" in data["summary"]
        assert "fallback / низкая уверенность" in data["items"][0]["reason"]
    finally:
        settings.market_import_token = old_token
        app.dependency_overrides.clear()


def test_avito_listing_evaluator_uses_usable_market_observations_for_auto_price(tmp_path) -> None:
    from app.main import app, get_db, settings

    database_url = f"sqlite:///{(tmp_path / 'evaluate-raw-market.sqlite').as_posix()}"
    engine = get_engine(database_url=database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        session.add(
            PriceObservation(
                source="adb_bot",
                source_uid="market-listing:test",
                observation_type="avito_market",
                item_title="Rare Test Game",
                price=9000,
                currency="RUB",
                observed_at=utcnow(),
                confidence=0.7,
                usable_for_auto_price=True,
                raw_json={
                    "item_type": "game",
                    "format": "physical",
                    "price_scope": "per_item",
                    "price_source_type": "description_item_price",
                    "price_confidence": "medium",
                },
            )
        )
        session.commit()

    def override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    old_token = settings.market_import_token
    settings.market_import_token = None
    try:
        client = TestClient(app)
        response = client.post(
            "/api/market/evaluate-avito-listing",
            json={
                "listing": {"price": 1000, "delivery_price_rub": 100},
                "extracted_items": [{"name": "Rare Test Game", "item_type": "game", "quantity": 1}],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["decision"] == "good"
        assert data["items"][0]["expected_sell_price"] == 9000
        assert data["items"][0]["source"] == "avito_market"
        assert data["items"][0]["confidence"] == "medium"
    finally:
        settings.market_import_token = old_token
        app.dependency_overrides.clear()


def test_avito_listing_evaluator_does_not_use_console_price_for_game(tmp_path) -> None:
    from app.main import app, get_db, settings

    database_url = f"sqlite:///{(tmp_path / 'evaluate-game-type-filter.sqlite').as_posix()}"
    engine = get_engine(database_url=database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        session.add_all(
            [
                PriceObservation(
                    source="adb_bot",
                    source_uid="market:ps4-console",
                    observation_type="avito_market",
                    item_title="PlayStation 4",
                    price=14000,
                    currency="RUB",
                    observed_at=utcnow(),
                    confidence=0.75,
                    usable_for_auto_price=True,
                    raw_json={
                        "canonical_name": "PlayStation 4",
                        "item_type": "console",
                        "format": "physical",
                        "price_scope": "per_item",
                        "price_source_type": "description_item_price",
                        "price_confidence": "high",
                    },
                ),
                PriceObservation(
                    source="adb_bot",
                    source_uid="market:mk11-game",
                    observation_type="avito_market",
                    item_title="Mortal Kombat 11",
                    price=1600,
                    currency="RUB",
                    observed_at=utcnow(),
                    confidence=0.75,
                    usable_for_auto_price=True,
                    raw_json={
                        "canonical_name": "Mortal Kombat 11",
                        "catalog_item_id": "mortal_kombat_11",
                        "item_type": "game",
                        "format": "physical",
                        "price_scope": "per_item",
                        "price_source_type": "description_item_price",
                        "price_confidence": "high",
                    },
                ),
            ]
        )
        session.commit()

    def override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    old_token = settings.market_import_token
    settings.market_import_token = None
    try:
        client = TestClient(app)
        response = client.post(
            "/api/market/evaluate-avito-listing",
            json={
                "listing": {"title": "Диск Mortal Kombat 11", "price": 1200},
                "extracted_items": [
                    {
                        "name": "Игра на Sony PlayStation 4",
                        "canonical_name": "Mortal Kombat 11",
                        "catalog_item_id": "mortal_kombat_11",
                        "platform": "PS4",
                        "item_type": "game",
                        "quantity": 1,
                    }
                ],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["items"][0]["expected_sell_price"] == 1600
        assert data["items"][0]["canonical_name"] == "Mortal Kombat 11"
        assert data["items"][0]["item_type"] == "game"
        assert data["items"][0]["expected_sell_price"] != 14000
    finally:
        settings.market_import_token = old_token
        app.dependency_overrides.clear()


def test_avito_listing_evaluator_does_not_use_game_price_for_controller(tmp_path) -> None:
    from app.main import app, get_db, settings

    database_url = f"sqlite:///{(tmp_path / 'evaluate-controller-type-filter.sqlite').as_posix()}"
    engine = get_engine(database_url=database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        session.add(
            PriceObservation(
                source="adb_bot",
                source_uid="market:horizon-game",
                observation_type="avito_market",
                item_title="Horizon Zero Dawn",
                price=1700,
                currency="RUB",
                observed_at=utcnow(),
                confidence=0.75,
                usable_for_auto_price=True,
                raw_json={
                    "canonical_name": "Horizon Zero Dawn",
                    "item_type": "game",
                    "format": "physical",
                    "price_scope": "per_item",
                    "price_source_type": "description_item_price",
                    "price_confidence": "high",
                },
            )
        )
        session.commit()

    def override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    old_token = settings.market_import_token
    settings.market_import_token = None
    try:
        client = TestClient(app)
        response = client.post(
            "/api/market/evaluate-avito-listing",
            json={
                "listing": {"title": "Геймпад PS4", "price": 1200},
                "extracted_items": [{"name": "Геймпад PS4", "item_type": "controller", "platform": "PS4"}],
            },
        )
        assert response.status_code == 200
        data = response.json()
        item = data["items"][0]
        assert item["input_name"] == "Геймпад PS4"
        assert item["input_item_type"] == "controller"
        assert item["canonical_name"] != "Horizon Zero Dawn"
        assert item["matched_catalog_name"] != "Horizon Zero Dawn"
        assert item["matched_catalog_type"] != "game"
    finally:
        settings.market_import_token = old_token
        app.dependency_overrides.clear()


def test_avito_listing_evaluator_rejects_unsafe_llm_catalog_match(db_session) -> None:
    from app.avito_evaluator import evaluate_avito_listing_payload

    data = evaluate_avito_listing_payload(
        db_session,
        listing={
            "title": "Uncharted collection ps4",
            "description": "Disc in good condition",
            "price": 1100,
        },
        extracted_items=[
            {
                "name": "Horizon Zero Dawn",
                "canonical_name": "Horizon Zero Dawn",
                "catalog_item_id": "horizon_zero_dawn",
                "item_type": "game",
            }
        ],
    )

    item = data["items"][0]
    assert item["name"] == "Uncharted collection ps4"
    assert item["canonical_name"] != "Horizon Zero Dawn"
    assert item["expected_sell_price"] is None
    assert item["matched_by"] is None
    assert "llm_item_name_not_found_in_listing_text" in data["risks"]


def test_avito_listing_evaluator_rejects_console_match_for_game_disc(db_session) -> None:
    from app.avito_evaluator import evaluate_avito_listing_payload

    data = evaluate_avito_listing_payload(
        db_session,
        listing={
            "title": "AD Infinitum PS5 (NEW)",
            "description": "Game Ad Infinitum for PS5, physical disc",
            "price": 2990,
        },
        extracted_items=[
            {
                "name": "PlayStation 5 Disc",
                "canonical_name": "PlayStation 5 Disc",
                "catalog_entry_id": 64,
                "item_type": "console",
            }
        ],
    )

    item = data["items"][0]
    assert item["name"] == "AD Infinitum PS5 (NEW)"
    assert item["item_type"] == "game"
    assert item["expected_sell_price"] is None
    assert item["matched_by"] is None
    assert "console_match_rejected_for_game_disc_listing" in data["risks"]


def test_avito_listing_evaluator_does_not_price_unknown_game_list(tmp_path) -> None:
    from app.main import app, get_db, settings

    database_url = f"sqlite:///{(tmp_path / 'evaluate-unknown-game-list.sqlite').as_posix()}"
    engine = get_engine(database_url=database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        session.add(
            PriceObservation(
                source="adb_bot",
                source_uid="market:horizon-game-unknown",
                observation_type="avito_market",
                item_title="Horizon Zero Dawn",
                price=1700,
                currency="RUB",
                observed_at=utcnow(),
                confidence=0.75,
                usable_for_auto_price=True,
                raw_json={
                    "canonical_name": "Horizon Zero Dawn",
                    "item_type": "game",
                    "format": "physical",
                    "price_scope": "per_item",
                    "price_source_type": "description_item_price",
                    "price_confidence": "high",
                },
            )
        )
        session.commit()

    def override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    old_token = settings.market_import_token
    settings.market_import_token = None
    try:
        client = TestClient(app)
        response = client.post(
            "/api/market/evaluate-avito-listing",
            json={
                "listing": {"title": "Unknown PS4 games", "price": 1200},
                "extracted_items": [{"name": "Unknown PS4 games", "item_type": "game", "platform": "PS4"}],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["decision"] == "skip"
        assert data["items"][0]["expected_sell_price"] is None
        assert data["items"][0]["matched_by"] is None
        assert data["items"][0]["match_score"] == 0.0
    finally:
        settings.market_import_token = old_token
        app.dependency_overrides.clear()


def test_avito_lot_cost_observations_are_buy_cost_not_market_price(tmp_path) -> None:
    from app.main import app, get_db, settings

    database_url = f"sqlite:///{(tmp_path / 'evaluate-lot-cost.sqlite').as_posix()}"
    engine = get_engine(database_url=database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        session.add_all(
            [
                PriceObservation(
                    source="adb_bot",
                    source_uid="market:gta",
                    observation_type="avito_market",
                    item_title="GTA 5",
                    price=1800,
                    currency="RUB",
                    observed_at=utcnow(),
                    confidence=0.75,
                    usable_for_auto_price=True,
                    raw_json={
                        "canonical_name": "Grand Theft Auto V",
                        "catalog_item_id": "gta_5",
                        "item_type": "game",
                        "format": "physical",
                        "price_scope": "per_item",
                        "price_source_type": "description_item_price",
                        "price_confidence": "high",
                    },
                ),
                PriceObservation(
                    source="adb_bot",
                    source_uid="market:ufc",
                    observation_type="avito_market",
                    item_title="UFC 4",
                    price=2200,
                    currency="RUB",
                    observed_at=utcnow(),
                    confidence=0.75,
                    usable_for_auto_price=True,
                    raw_json={
                        "item_type": "game",
                        "catalog_item_id": "ufc_4",
                        "format": "physical",
                        "price_scope": "per_item",
                        "price_source_type": "description_item_price",
                        "price_confidence": "high",
                    },
                ),
            ]
        )
        session.commit()

    def override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    old_token = settings.market_import_token
    settings.market_import_token = None
    try:
        client = TestClient(app)
        response = client.post(
            "/api/market/evaluate-avito-listing",
            json={
                "listing": {
                    "title": "Игры PS4 лотом",
                    "price": 5000,
                    "delivery_price_rub": 200,
                    "raw_json": {
                        "llm_lot_cost_observations": [
                            {"name": "GTA 5", "canonical_name": "Grand Theft Auto V", "catalog_item_id": "gta_5", "platform": "PS4", "item_type": "game", "lot_unit_buy_price_rub": 1000},
                            {"name": "UFC 4", "catalog_item_id": "ufc_4", "platform": "PS4", "item_type": "game", "lot_unit_buy_price_rub": 1000},
                        ],
                        "llm_listing_price_interpretation": {
                            "listing_price_rub": 5000,
                            "role": "lot_or_bundle_total_price",
                            "can_use_listing_price_for_market_pricing": False,
                        },
                    },
                }
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["total"]["buy_total"] == 5200
        assert data["total"]["expected_sell_total"] == 4000
        assert data["total"]["expected_profit"] == -1720
        assert data["items"][0]["buy_price"] == 1100
        assert data["items"][0]["expected_sell_price"] == 1800
        assert data["items"][0]["price_source"] == "market_observations_30d"
        assert "Себестоимость из лота" in data["items"][0]["reason"]
    finally:
        settings.market_import_token = old_token
        app.dependency_overrides.clear()


def test_avito_lot_listing_price_is_not_imported_as_market_observation(db_session) -> None:
    result = import_avito_api_batch(
        db_session,
        source="adb_bot",
        filename="adb_bot_live",
        scraped_at=None,
        listings=[
            {
                "external_id": "lot-1",
                "title": "Игры PS4 лотом",
                "url": "https://www.avito.ru/item/lot-1",
                "price": 5000,
                "platform": "PS4",
                "format": "physical",
                "type": "game_lot",
                "raw_json": {
                    "source": "adb_bot",
                    "fingerprint": "ps4-lot|5000",
                    "llm_market_price_observations": [],
                    "llm_lot_cost_observations": [{"name": "GTA 5", "lot_unit_buy_price_rub": 1000}],
                    "llm_listing_price_interpretation": {
                        "role": "lot_or_bundle_total_price",
                        "can_use_listing_price_for_market_pricing": False,
                    },
                },
            }
        ],
    )
    db_session.flush()

    listing = db_session.get(MarketListing, result.created_listing_ids[0])
    assert listing is not None
    assert db_session.scalar(select(func.count(PriceObservation.id)).where(PriceObservation.market_listing_id == listing.id)) == 0


def test_history_event_icons_match_event_types() -> None:
    from app.main import history_event_icon

    assert history_event_icon("Закупка", "event-purchase") == "📦"
    assert history_event_icon("Добавление", "event-create") == "📦"
    assert history_event_icon("Продажа", "event-sale") == "🤝"
    assert history_event_icon("Изменение", "event-update") == "✏️"
    assert history_event_icon("Удаление", "event-delete") == "🗑️"
    assert history_event_icon("Возврат", "event-return") == "🔄"


def test_history_events_use_item_purchase_date(db_session) -> None:
    from app.main import inventory_history_events

    seed_database(db_session)
    games = _game_category(db_session)
    item = create_item(
        db_session,
        item_input=ItemInput(category_id=games.id, catalog_name="Ghost of Tsushima", calculated_cost=1000),
    )
    db_session.flush()

    events = inventory_history_events([item], limit=20)
    kinds = {event["kind"] for event in events}

    assert "Наличие" not in kinds
    assert "Закупка" in kinds
    assert "Добавление" not in kinds


def test_history_uses_purchase_event_for_manual_item(db_session) -> None:
    from app.main import inventory_history_events

    seed_database(db_session)
    games = _game_category(db_session)
    item = create_item(
        db_session,
        item_input=ItemInput(category_id=games.id, catalog_name="Manual Card", quantity=1, calculated_cost=500),
    )
    db_session.flush()

    events = inventory_history_events([item], limit=20)
    kinds = {event["kind"] for event in events}

    assert "Закупка" in kinds
    assert "Добавление" not in kinds


def test_analysis_export_import_does_not_create_inventory_items(tmp_path, monkeypatch) -> None:
    from app.main import app, get_db
    import app.analysis_exchange as analysis_exchange

    queue_path = tmp_path / "current_export.json"
    monkeypatch.setattr(analysis_exchange, "CURRENT_EXPORT_PATH", queue_path)

    database_url = f"sqlite:///{(tmp_path / 'exchange-flow.sqlite').as_posix()}"
    engine = get_engine(database_url=database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        seed_database(session)
        games = _game_category(session)
        item = create_item(
            session,
            item_input=ItemInput(category_id=games.id, catalog_name="Ghost of Tsushima", calculated_cost=1000),
        )
        sell_item(session, item_id=item.id, sale_price=1700, sold_at=None)
        session.commit()

    def override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    export_response = client.post("/exchange/export", follow_redirects=False)
    assert export_response.status_code == 303
    assert queue_path.exists()
    assert queue_path.read_text(encoding="utf-8").lstrip().startswith("{")
    payload = json.loads(queue_path.read_text(encoding="utf-8"))
    assert "purchase_ids" not in payload.get("sync", {})
    assert payload["items"][0]["purchased_at"] is None
    assert "source" in payload["items"][0]
    assert "source_url" in payload["items"][0]
    with session_factory() as session:
        item = session.scalar(select(Item).where(Item.code.is_not(None)))
        assert item.is_synced is False
        analysis_exchange.mark_export_payload_synced(session, payload)
        session.commit()
    with session_factory() as session:
        item = session.scalar(select(Item).where(Item.code.is_not(None)))
        assert item.is_synced is True

    imported = client.post(
        "/exchange/import",
        files={"file": ("current_export.json", queue_path.read_bytes(), "application/json")},
        follow_redirects=False,
    )
    assert imported.status_code == 303

    with session_factory() as session:
        assert session.scalar(select(func.count(Item.id))) == 1
        assert session.scalar(select(func.count(AnalysisItem.id))) == 1
        analysis_item = session.scalar(select(AnalysisItem))
        assert analysis_item.title == "Ghost of Tsushima"
        assert analysis_item.sale_price == 1700
        observation = session.scalar(select(PriceObservation).where(PriceObservation.analysis_item_id == analysis_item.id))
        assert observation is not None
        assert observation.observation_type == "partner_sale"
        assert observation.price == 1700

    page = client.get("/history")
    assert page.status_code == 200
    assert "История импортов" in page.text
    assert "Ghost of Tsushima" in page.text

    deleted = client.post("/exchange/imports/1/delete", follow_redirects=False)
    assert deleted.status_code == 303
    with session_factory() as session:
        assert session.scalar(select(func.count(Item.id))) == 1
        assert session.scalar(select(func.count(AnalysisItem.id))) == 0
        assert session.scalar(select(func.count(PriceObservation.id))) == 0
    app.dependency_overrides.clear()


def test_sold_item_can_be_returned_to_available_from_web(tmp_path) -> None:
    from app.main import app, get_db

    database_url = f"sqlite:///{(tmp_path / 'return-flow.sqlite').as_posix()}"
    engine = get_engine(database_url=database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        seed_database(session)
        games = _game_category(session)
        item = create_item(
            session,
            item_input=ItemInput(category_id=games.id, catalog_name="Ghost of Tsushima", calculated_cost=1000),
        )
        sell_item(session, item_id=item.id, sale_price=1700, sold_at=None)
        item_id = item.id
        session.commit()

    def override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    returned = client.post(f"/items/{item_id}/available", follow_redirects=False)

    assert returned.status_code == 303
    assert returned.headers["location"] == "/bought"
    with session_factory() as session:
        item = session.get(Item, item_id)
        assert item.status == "Есть"
        assert item.sale_price is None
        assert item.sold_at is None
        assert item.is_synced is False
    app.dependency_overrides.clear()


def test_single_disc_web_flow_creates_bought_item(tmp_path) -> None:
    from app.main import app, get_db

    database_url = f"sqlite:///{(tmp_path / 'disc-flow.sqlite').as_posix()}"
    engine = get_engine(database_url=database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        seed_database(session)
        session.commit()

    def override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    response = client.post(
        "/bought/new-disc",
        data={
            "catalog_name": "Call of Duty: Modern Warfare 2",
            "platform": "PlayStation 5",
            "localization": "ru_subs_ui",
            "disc_surface": "Без заметных царапин",
            "test_result": "Работает",
            "completeness": "Диск в коробке",
            "calculated_cost": "1200",
            "expected_sale_price": "1800",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/bought?created=ITEM-")
    with session_factory() as session:
        item = session.scalar(select(Item).where(Item.code.is_not(None)))
        assert item is not None
        item_id = item.id
        assert item.code == "ITEM-000001"
        assert item.calculated_cost == 1200
        assert item.expected_sale_price == 1800
        assert item.game_detail.platform == "PlayStation 5"
        assert item.game_detail.voice_language == "Английский"

    page = client.get("/bought")
    assert page.status_code == 200
    assert "000001" in page.text
    assert "ITEM-000001" not in page.text
    assert "Call of Duty: Modern Warfare 2" in page.text
    assert "Удалить" in page.text
    assert "data-delete-form" in page.text
    assert "confirm(" not in page.text

    inventory = client.get("/items", follow_redirects=False)
    assert inventory.status_code == 303
    assert inventory.headers["location"] == "/bought"

    edit_page = client.get(f"/items/{item_id}/edit")
    assert edit_page.status_code == 200
    assert "Тип аксессуара" not in edit_page.text
    assert "Память" not in edit_page.text

    edited = client.post(
        f"/items/{item_id}/edit",
        data={
            "catalog_name": "Ghost of Tsushima",
            "platform": "PlayStation 4",
            "localization": "full_ru",
            "disc_surface": "Есть царапины",
            "test_result": "Работает",
            "completeness": "Диск в коробке",
            "calculated_cost": "1000",
            "expected_sale_price": "1600",
            "source": "Авито",
        },
        follow_redirects=False,
    )
    assert edited.status_code == 303
    assert edited.headers["location"] == "/bought"
    with session_factory() as session:
        item = session.get(Item, item_id)
        assert item.game_detail.platform == "PlayStation 4"
        assert item.game_detail.voice_language == "Русский"
        assert item.calculated_cost == 1000

    deleted = client.post(f"/items/{item_id}/delete", follow_redirects=False)
    assert deleted.status_code == 303
    assert deleted.headers["location"] == "/bought"
    with session_factory() as session:
        assert session.get(Item, item_id) is None
    app.dependency_overrides.clear()


def test_stats_shows_rough_stock_profit_from_zip(tmp_path) -> None:
    from app.main import app, get_db, inventory_items, inventory_stats

    database_url = f"sqlite:///{(tmp_path / 'rough-profit.sqlite').as_posix()}"
    engine = get_engine(database_url=database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        seed_database(session)
        games = _game_category(session)
        create_item(
            session,
            item_input=ItemInput(category_id=games.id, catalog_name="Red Dead Redemption 2", calculated_cost=950),
        )
        session.commit()
    with session_factory() as session:
        stats = inventory_stats(inventory_items(session))
        assert stats["rough_stock"]["matched_count"] == 1
        assert stats["rough_stock"]["estimated_profit"] > 0
        assert stats["capital_category_rows"][0]["stock_cost"] == 950
        assert stats["pricing_rows"][0]["minimum_price"] >= 950
        assert stats["pricing_rows"][0]["auto_price"] is not None
        assert stats["sell_priority_rows"][0]["score"] >= 0

    def override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    page = client.get("/stats")

    assert page.status_code == 200
    assert "Бизнес-коэффициенты" in page.text
    assert "Ценовые источники" in page.text
    assert "Примерный профит остатка" in page.text
    assert "Фактический профит по проданному" in page.text
    assert "Капитал по категориям" in page.text
    assert "Минимальная цена / автоцена" in page.text
    assert "Что продавать первым" in page.text
    app.dependency_overrides.clear()


def test_auto_price_uses_weighted_median_observations_with_market(db_session) -> None:
    from app.main import inventory_items, inventory_stats

    seed_database(db_session)
    games = _game_category(db_session)
    item = create_item(
        db_session,
        item_input=ItemInput(category_id=games.id, catalog_name="Median Price Game", calculated_cost=100),
    )
    db_session.add_all(
        [
            PriceObservation(
                source="crm",
                source_uid="own-sale:median",
                observation_type="own_sale",
                catalog_entry_id=item.catalog_entry_id,
                item_title="Median Price Game",
                price=1000,
                currency="RUB",
                observed_at=utcnow(),
                confidence=1.0,
                usable_for_auto_price=True,
                raw_json={"item_type": "game"},
            ),
            PriceObservation(
                source="artem",
                source_uid="partner-sale:median",
                observation_type="partner_sale",
                catalog_entry_id=item.catalog_entry_id,
                item_title="Median Price Game",
                price=3000,
                currency="RUB",
                observed_at=utcnow(),
                confidence=0.85,
                usable_for_auto_price=True,
                raw_json={"item_type": "game"},
            ),
            PriceObservation(
                source="adb_bot",
                source_uid="market-listing:median",
                observation_type="avito_market",
                catalog_entry_id=item.catalog_entry_id,
                item_title="Median Price Game",
                price=9000,
                currency="RUB",
                observed_at=utcnow(),
                confidence=0.7,
                usable_for_auto_price=True,
                raw_json={
                    "item_type": "game",
                    "format": "physical",
                    "price_scope": "per_item",
                    "price_source_type": "description_item_price",
                    "price_confidence": "medium",
                },
            ),
        ]
    )
    db_session.flush()

    stats = inventory_stats(inventory_items(db_session), db_session)

    assert stats["pricing_rows"][0]["auto_price"] == 3000
    assert stats["pricing_rows"][0]["source"] == "Твои продажи"


def test_zero_cost_web_flow_shows_free_label(tmp_path) -> None:
    from app.main import app, get_db

    database_url = f"sqlite:///{(tmp_path / 'free-disc-flow.sqlite').as_posix()}"
    engine = get_engine(database_url=database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        seed_database(session)
        session.commit()

    def override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    response = client.post(
        "/bought/new-disc",
        data={
            "disc_title": "Ghost of Tsushima",
            "platform": "PlayStation 4",
            "localization": "full_ru",
            "completeness": "Диск в коробке",
            "calculated_cost": "0",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    with session_factory() as session:
        item = session.scalar(select(Item).where(Item.code.is_not(None)))
        assert item.calculated_cost == 0

    page = client.get("/bought")
    assert "Бесплатно" in page.text
    app.dependency_overrides.clear()


def test_console_web_flow_creates_console_detail(tmp_path) -> None:
    from app.main import app, get_db

    database_url = f"sqlite:///{(tmp_path / 'console-flow.sqlite').as_posix()}"
    engine = get_engine(database_url=database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        seed_database(session)
        session.commit()

    def override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    response = client.post(
        "/bought/new-disc",
        data={
            "disc_title": "PlayStation 4",
            "console_version": "Slim",
            "storage_size": "1 ТБ",
            "controllers_count": "1",
            "console_condition": "Работает",
            "console_complete": "no",
            "missing_parts": ["HDMI-кабель", "Кабель питания"],
            "calculated_cost": "9500",
            "expected_sale_price": "12000",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    with session_factory() as session:
        item = session.scalar(select(Item).where(Item.code.is_not(None)))
        item_id = item.id
        assert item.console_detail is not None
        assert item.game_detail is None
        assert item.console_detail.model == "Slim"
        assert item.console_detail.storage_size == "1 ТБ"
        assert item.console_detail.controllers_count == 1
        assert item.console_detail.completeness == "Некомплект"
        assert "HDMI-кабель" in item.console_detail.comment

    page = client.get("/bought")
    assert page.status_code == 200
    assert "PlayStation 4" in page.text
    assert "Slim" in page.text
    assert "HDMI-кабель" in page.text

    edit_page = client.get(f"/items/{item_id}/edit")
    assert edit_page.status_code == 200
    assert "Версия" in edit_page.text
    assert "Объём памяти" in edit_page.text

    edited = client.post(
        f"/items/{item_id}/edit",
        data={
            "disc_title": "PlayStation 5",
            "console_version": "Pro",
            "storage_size": "2 ТБ",
            "controllers_count": "2",
            "console_condition": "Работает",
            "console_completeness": "Полный комплект",
            "calculated_cost": "11000",
            "expected_sale_price": "14000",
            "source": "Авито",
        },
        follow_redirects=False,
    )
    assert edited.status_code == 303
    assert edited.headers["location"] == "/bought"
    with session_factory() as session:
        item = session.get(Item, item_id)
        assert item.console_detail.model == "Pro"
        assert item.console_detail.storage_size == "2 ТБ"
        assert item.console_detail.controllers_count == 2
        assert item.calculated_cost == 11000
    app.dependency_overrides.clear()


def test_accessory_gamepad_web_flow_creates_accessory_detail(tmp_path) -> None:
    from app.main import app, get_db

    database_url = f"sqlite:///{(tmp_path / 'accessory-flow.sqlite').as_posix()}"
    engine = get_engine(database_url=database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        seed_database(session)
        accessory_category = _category(session, ACCESSORY_CATEGORY)
        accessory_category_id = accessory_category.id
        session.commit()

    def override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    response = client.post(
        "/bought/new-disc",
        data={
            "category_id": str(accessory_category_id),
            "disc_title": "DualShock 4",
            "accessory_type": "Геймпад",
            "visible_wear": "yes",
            "originality": "Оригинальный",
            "calculated_cost": "1800",
            "expected_sale_price": "2500",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    with session_factory() as session:
        item = session.scalar(select(Item).where(Item.code.is_not(None)))
        item_id = item.id
        assert item.accessory_detail is not None
        assert item.game_detail is None
        assert item.accessory_detail.accessory_type == "Геймпад"
        assert item.accessory_detail.originality == "Оригинальный"
        assert item.accessory_detail.condition == "Есть видимые следы использования"

    page = client.get("/bought")
    assert page.status_code == 200
    assert "DualShock 4" in page.text
    assert "Оригинальный" in page.text

    edit_page = client.get(f"/items/{item_id}/edit")
    assert edit_page.status_code == 200
    assert "Тип аксессуара" in edit_page.text
    assert "Оригинальность" in edit_page.text

    edited = client.post(
        f"/items/{item_id}/edit",
        data={
            "disc_title": "Зарядная станция DualShock 4",
            "accessory_type": "Зарядная станция",
            "visible_wear": "no",
            "calculated_cost": "1500",
            "expected_sale_price": "2300",
            "source": "Авито",
        },
        follow_redirects=False,
    )
    assert edited.status_code == 303
    assert edited.headers["location"] == "/bought"
    with session_factory() as session:
        item = session.get(Item, item_id)
        assert item.accessory_detail.accessory_type == "Зарядная станция"
        assert item.accessory_detail.originality is None
        assert item.accessory_detail.condition == "Без видимых следов использования"
        assert item.calculated_cost == 1500
    app.dependency_overrides.clear()


def test_data_persists_after_new_session(tmp_path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'persist.sqlite').as_posix()}"
    engine = get_engine(database_url=database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        seed_database(session)
        games = session.scalar(select(Category).where(Category.name == GAME_CATEGORY))
        create_item(
            session,
            item_input=ItemInput(category_id=games.id, catalog_name="Ghost of Tsushima"),
        )
        session.commit()
    with session_factory() as session:
        assert session.scalar(select(func.count(Item.id))) == 1
        assert dashboard_stats(session).available_items == 1

