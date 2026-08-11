"""Database setup helpers."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg
from psycopg import sql
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.config import RUNTIME_DIR, Settings, get_settings
from app.models import Base


def sqlite_path_from_url(database_url: str) -> Path:
    path = Path(database_url.removeprefix("sqlite:///"))
    if not path.is_absolute():
        path = RUNTIME_DIR / path
    return path


def is_sqlite_url(database_url: str) -> bool:
    return make_url(database_url).drivername.startswith("sqlite")


def get_engine(settings: Settings | None = None, database_url: str | None = None) -> Engine:
    if database_url is None:
        settings = settings or get_settings()
        database_url = settings.database_url
    if is_sqlite_url(database_url):
        database_path = sqlite_path_from_url(database_url)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        database_url = "sqlite:///" + database_path.as_posix()
    else:
        ensure_postgres_database(database_url)
    return create_engine(database_url, future=True)


def ensure_postgres_database(database_url: str) -> None:
    url = make_url(database_url)
    if not url.drivername.startswith("postgresql"):
        return
    database = url.database
    if not database:
        return
    admin_url = url.set(database="postgres", drivername="postgresql")
    conninfo = admin_url.render_as_string(hide_password=False)
    try:
        with psycopg.connect(conninfo, autocommit=True) as conn:
            exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,)).fetchone()
            if not exists:
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    except psycopg.OperationalError:
        return


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    ensure_runtime_columns(engine)


def ensure_runtime_columns(engine: Engine) -> None:
    inspector = inspect(engine)
    if "items" not in inspector.get_table_names():
        return

    datetime_type = "TIMESTAMP" if engine.dialect.name == "postgresql" else "DATETIME"
    column_names = {column["name"] for column in inspector.get_columns("items")}
    default = "false" if engine.dialect.name == "postgresql" else "0"
    column_definitions = {
        "purchased_at": f"{datetime_type} NULL",
        "source": "VARCHAR(100) NULL",
        "source_url": "TEXT NULL",
        "is_synced": f"BOOLEAN NOT NULL DEFAULT {default}",
    }
    with engine.begin() as connection:
        for column_name, definition in column_definitions.items():
            if column_name not in column_names:
                connection.execute(text(f"ALTER TABLE items ADD COLUMN {column_name} {definition}"))

    ensure_market_import_columns(engine, datetime_type)
    ensure_market_listing_columns(engine, datetime_type)
    ensure_price_observation_columns(engine)
    migrate_purchase_fields_to_items(engine)


def ensure_market_import_columns(engine: Engine, datetime_type: str) -> None:
    inspector = inspect(engine)
    if "market_imports" not in inspector.get_table_names():
        return

    column_names = {column["name"] for column in inspector.get_columns("market_imports")}
    column_definitions = {
        "scraped_at": f"{datetime_type} NULL",
        "crm_sent_at": f"{datetime_type} NULL",
    }
    with engine.begin() as connection:
        for column_name, definition in column_definitions.items():
            if column_name not in column_names:
                connection.execute(text(f"ALTER TABLE market_imports ADD COLUMN {column_name} {definition}"))


def ensure_market_listing_columns(engine: Engine, datetime_type: str) -> None:
    inspector = inspect(engine)
    if "market_listings" not in inspector.get_table_names():
        return

    column_names = {column["name"] for column in inspector.get_columns("market_listings")}
    column_definitions = {
        "exported_at": f"{datetime_type} NULL",
    }
    with engine.begin() as connection:
        for column_name, definition in column_definitions.items():
            if column_name not in column_names:
                connection.execute(text(f"ALTER TABLE market_listings ADD COLUMN {column_name} {definition}"))


def ensure_price_observation_columns(engine: Engine) -> None:
    inspector = inspect(engine)
    if "price_observations" not in inspector.get_table_names():
        return

    column_names = {column["name"] for column in inspector.get_columns("price_observations")}
    default = "false" if engine.dialect.name == "postgresql" else "0"
    with engine.begin() as connection:
        if "usable_for_auto_price" not in column_names:
            connection.execute(
                text(f"ALTER TABLE price_observations ADD COLUMN usable_for_auto_price BOOLEAN NOT NULL DEFAULT {default}")
            )


def migrate_purchase_fields_to_items(engine: Engine) -> None:
    inspector = inspect(engine)
    public_tables = set(inspector.get_table_names())
    if "items" not in public_tables or "purchases" not in public_tables:
        return

    item_columns = {column["name"] for column in inspector.get_columns("items")}
    if "purchase_id" in item_columns:
        with engine.begin() as connection:
            if engine.dialect.name == "postgresql":
                connection.execute(
                    text(
                        """
                        UPDATE items
                           SET purchased_at = COALESCE(items.purchased_at, purchases.purchased_at),
                               source = COALESCE(items.source, purchases.source),
                               source_url = COALESCE(items.source_url, purchases.source_url)
                          FROM purchases
                         WHERE items.purchase_id = purchases.id
                        """
                    )
                )
            else:
                connection.execute(
                    text(
                        """
                        UPDATE items
                           SET purchased_at = COALESCE(
                                   purchased_at,
                                   (SELECT purchases.purchased_at FROM purchases WHERE purchases.id = items.purchase_id)
                               ),
                               source = COALESCE(
                                   source,
                                   (SELECT purchases.source FROM purchases WHERE purchases.id = items.purchase_id)
                               ),
                               source_url = COALESCE(
                                   source_url,
                                   (SELECT purchases.source_url FROM purchases WHERE purchases.id = items.purchase_id)
                               )
                         WHERE purchase_id IS NOT NULL
                        """
                    )
                )

    if engine.dialect.name == "postgresql":
        archive_purchase_table(engine)


def archive_purchase_table(engine: Engine) -> None:
    with engine.begin() as connection:
        public_exists = connection.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                      FROM information_schema.tables
                     WHERE table_schema = 'public'
                       AND table_name = 'purchases'
                )
                """
            )
        ).scalar()
        archive_exists = connection.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                      FROM information_schema.tables
                     WHERE table_schema = 'archive'
                       AND table_name = 'purchases'
                )
                """
            )
        ).scalar()
        if not public_exists:
            return

        connection.execute(text("CREATE SCHEMA IF NOT EXISTS archive"))
        connection.execute(
            text(
                """
                DO $$
                DECLARE
                    constraint_name text;
                BEGIN
                    FOR constraint_name IN
                        SELECT conname
                          FROM pg_constraint
                         WHERE conrelid = 'public.items'::regclass
                           AND contype = 'f'
                           AND pg_get_constraintdef(oid) LIKE '%purchase_id%'
                    LOOP
                        EXECUTE format('ALTER TABLE public.items DROP CONSTRAINT %I', constraint_name);
                    END LOOP;
                END $$;
                """
            )
        )
        connection.execute(text("DROP INDEX IF EXISTS public.ix_items_purchase_id"))
        connection.execute(text("ALTER TABLE public.items DROP COLUMN IF EXISTS purchase_id"))
        if not archive_exists:
            connection.execute(text("ALTER TABLE public.purchases SET SCHEMA archive"))


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
