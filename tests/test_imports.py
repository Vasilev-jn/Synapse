def test_transferred_modules_import():
    import app.catalog_search
    import app.crm_market
    import app.env
    import app.listing_db
    import app.llm_analyzer
    import app.main
    import app.notifier
    import app.profit_estimator

    assert app.catalog_search.CATALOG_PATH.exists()
    assert app.profit_estimator.CRM_GAME_SUGGESTIONS_PATH.exists()
