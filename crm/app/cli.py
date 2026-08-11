"""Command line interface for the CRM."""

from __future__ import annotations

import time
from typing import Any

import typer

from app.analysis_exchange import (
    build_incremental_analysis_export_file,
    import_received_json_files,
    receive_once_via_croc,
    send_current_export_via_croc,
)
from app.config import Settings, ensure_data_dirs, get_settings
from app.db import get_engine, init_db, make_session_factory, session_scope
from app.db_transfer import migrate_sqlite_to_database
from app.deduplication import deduplicate_all
from app.exceptions import ScraperError
from app.inventory import dashboard_stats, export_items_csv, seed_database
from app.market import import_market_files


app = typer.Typer(no_args_is_help=True)


def _settings_and_session() -> tuple[Settings, Any]:
    ensure_data_dirs()
    settings = get_settings()
    engine = get_engine(settings=settings)
    init_db(engine)
    return settings, make_session_factory(engine)


@app.command("init-db")
def init_db_cmd() -> None:
    """Create database tables."""

    ensure_data_dirs()
    settings = get_settings()
    engine = get_engine(settings=settings)
    init_db(engine)
    typer.echo(f"Database initialized: {settings.database_label}")


@app.command("seed")
def seed_cmd() -> None:
    """Create system categories and preset catalog values. Idempotent."""

    _settings, session_factory = _settings_and_session()
    with session_scope(session_factory) as session:
        seed_database(session)
    typer.echo("Seed completed.")


@app.command("dedupe")
def dedupe_cmd() -> None:
    """Merge technical duplicates without deleting real duplicate physical items."""

    _settings, session_factory = _settings_and_session()
    with session_scope(session_factory) as session:
        stats = deduplicate_all(session)
    typer.echo(f"Categories merged: {stats.categories_merged}")
    typer.echo(f"Catalog entries merged: {stats.catalog_entries_merged}")
    typer.echo(f"Aliases removed: {stats.aliases_removed}")
    typer.echo(f"Market matches removed: {stats.market_matches_removed}")
    typer.echo(f"Analysis items merged: {stats.analysis_items_merged}")
    typer.echo(f"Item codes fixed: {stats.item_codes_fixed}")
    typer.echo(f"Total changes: {stats.total_changes}")


@app.command("sync-build-export")
def sync_build_export_cmd() -> None:
    """Build ./queue/current_export.json from unsynced inventory rows."""

    settings, session_factory = _settings_and_session()
    with session_scope(session_factory) as session:
        result = build_incremental_analysis_export_file(session, settings)
    if result.path is None:
        typer.echo("No unsynced rows. Queue is empty.")
    else:
        typer.echo(f"Queued: {result.exported_count} rows -> {result.path}")


@app.command("sync-send-worker")
def sync_send_worker_cmd(
    interval_seconds: int = typer.Option(300, "--interval-seconds", min=10, help="Queue polling interval."),
    timeout_seconds: int = typer.Option(90, "--timeout-seconds", min=10, help="Timeout for one croc send attempt."),
    once: bool = typer.Option(False, "--once", help="Run one queue check and exit."),
) -> None:
    """Continuously send ./queue/current_export.json to other croc users."""

    settings, session_factory = _settings_and_session()
    while True:
        with session_scope(session_factory) as session:
            result = send_current_export_via_croc(session, settings, timeout_seconds=timeout_seconds)
        if result is None:
            typer.echo("Queue is empty.")
        else:
            statuses = ", ".join(f"{target.user_id}:{'ok' if target.success else 'fail'}" for target in result.targets)
            typer.echo(f"Send attempt: {statuses or 'no targets'}; synced={len(result.synced_item_ids)}")
        if once:
            return
        time.sleep(interval_seconds)


@app.command("sync-receive-worker")
def sync_receive_worker_cmd(
    once: bool = typer.Option(False, "--once", help="Receive one croc transfer and exit."),
) -> None:
    """Continuously receive croc JSON files into analysis tables."""

    settings, session_factory = _settings_and_session()
    while True:
        received = receive_once_via_croc(settings)
        with session_scope(session_factory) as session:
            results = import_received_json_files(session)
        typer.echo(f"Received files: {len(received)}; imported batches: {len(results)}")
        if once:
            return


@app.command("sync-install-windows-tasks")
def sync_install_windows_tasks_cmd(
    task_name: str = typer.Option("CRM P2P Build Export", "--task-name", help="Windows Task Scheduler task name."),
) -> None:
    """Install Windows tasks for export generation, sending and receiving."""

    from app.desktop import install_windows_tasks

    install_windows_tasks()
    typer.echo(f"Scheduled CRM sync tasks. Legacy task-name option ignored: {task_name}.")


@app.command("migrate-to-postgres")
def migrate_to_postgres_cmd(
    source: str = typer.Option("sqlite:///data/avito_scraper.db", "--source", help="Source SQLite DATABASE_URL."),
    target: str = typer.Option(..., "--target", help="Target PostgreSQL SQLAlchemy URL."),
    replace: bool = typer.Option(False, "--replace", help="Clear target CRM tables before copying."),
) -> None:
    """Copy the current SQLite CRM database into PostgreSQL."""

    try:
        stats = migrate_sqlite_to_database(source, target, replace=replace)
    except Exception as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Source: {stats.source_url}")
    typer.echo(f"Target: {stats.target_url}")
    typer.echo(f"Rows copied: {stats.total_rows}")
    for table_name, count in stats.table_counts.items():
        if count:
            typer.echo(f"  {table_name}: {count}")
    typer.echo(f"Dedup changes before copy: {stats.dedup_stats.total_changes}")


@app.command("import-market")
def import_market_cmd(
    files: list[str] = typer.Argument(..., help="One or more JSON files or glob patterns, e.g. data/import/*.json"),
) -> None:
    """Import local Avito JSON files into market tables without calling Apify."""

    _settings, session_factory = _settings_and_session()
    try:
        with session_scope(session_factory) as session:
            stats = import_market_files(session, files)
    except (ValueError, ScraperError) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Files: {stats.files_count}")
    typer.echo(f"Raw rows: {stats.raw_count}")
    typer.echo(f"Unique listings total: {stats.unique_count}")
    typer.echo(f"Duplicates: {stats.duplicate_count}")
    typer.echo(f"Physical: {stats.physical_count}")
    typer.echo(f"Digital: {stats.digital_count}")
    typer.echo(f"Subscriptions: {stats.subscription_count}")
    typer.echo(f"Private sellers: {stats.private_count}")
    typer.echo(f"Companies: {stats.company_count}")
    typer.echo(f"Auto matched games: {stats.auto_matched_count}")
    typer.echo(f"Ambiguous rows: {stats.ambiguous_count}")


@app.command("stats")
def stats_cmd() -> None:
    """Show local inventory and market statistics."""

    _settings, session_factory = _settings_and_session()
    with session_scope(session_factory) as session:
        stats = dashboard_stats(session)
    typer.echo(f"Товары: {stats.available_items + stats.sold_items}")
    typer.echo(f"Доступные товары: {stats.available_items}")
    typer.echo(f"Проданные товары: {stats.sold_items}")
    typer.echo(f"Рыночные объявления: {stats.market_listings_count}")
    typer.echo(f"Сопоставленные объявления: {stats.matched_market_count}")
    typer.echo(f"Текущие вложения: {stats.stock_cost_total:.2f}")
    typer.echo(f"Выручка: {stats.revenue_total:.2f}")
    typer.echo(f"Прибыль: {stats.realized_profit if stats.realized_profit is not None else 'неизвестно'}")


@app.command("export-items")
def export_items_cmd() -> None:
    """Export inventory items to CSV."""

    _settings, session_factory = _settings_and_session()
    with session_scope(session_factory) as session:
        path = export_items_csv(session)
    typer.echo(f"Items CSV: {path}")


if __name__ == "__main__":
    app()
