"""Database migration helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.engine import Engine

from app.db import ensure_postgres_database, get_engine, init_db
from app.deduplication import DedupStats, deduplicate_all
from app.models import Base


@dataclass
class MigrationStats:
    source_url: str
    target_url: str
    table_counts: dict[str, int]
    dedup_stats: DedupStats
    replaced: bool = False

    @property
    def total_rows(self) -> int:
        return sum(self.table_counts.values())


def migrate_sqlite_to_database(source_url: str, target_url: str, *, replace: bool = False) -> MigrationStats:
    source_engine = get_engine(database_url=source_url)
    ensure_postgres_database(target_url)
    target_engine = create_engine(target_url, future=True)
    init_db(source_engine)
    init_db(target_engine)

    dedup_stats = run_source_dedup(source_url)

    table_counts: dict[str, int] = {}
    with source_engine.connect() as source_conn, target_engine.begin() as target_conn:
        if database_has_rows(target_engine):
            if not replace:
                raise ValueError("Целевая база не пустая. Запустите с --replace, если её можно перезаписать.")
            clear_database(target_conn)
        for table in Base.metadata.sorted_tables:
            rows = [dict(row) for row in source_conn.execute(select(table)).mappings()]
            table_counts[table.name] = len(rows)
            if rows:
                target_conn.execute(table.insert(), rows)
        reset_postgres_sequences(target_engine, target_conn)
    return MigrationStats(source_url=source_url, target_url=target_url, table_counts=table_counts, dedup_stats=dedup_stats, replaced=replace)


def run_source_dedup(source_url: str) -> DedupStats:
    engine = get_engine(database_url=source_url)
    from app.db import make_session_factory

    session_factory = make_session_factory(engine)
    with session_factory() as session:
        stats = deduplicate_all(session)
        session.commit()
    return stats


def database_has_rows(engine: Engine) -> bool:
    with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            count = conn.execute(select(func.count()).select_from(table)).scalar_one()
            if count:
                return True
    return False


def clear_database(conn: Any) -> None:
    for table in reversed(Base.metadata.sorted_tables):
        conn.execute(delete(table))


def reset_postgres_sequences(engine: Engine, conn: Any) -> None:
    if not engine.dialect.name.startswith("postgresql"):
        return
    for table in Base.metadata.sorted_tables:
        pk_columns = list(table.primary_key.columns)
        if len(pk_columns) != 1:
            continue
        pk = pk_columns[0]
        sequence = conn.execute(
            text("SELECT pg_get_serial_sequence(:table_name, :column_name)"),
            {"table_name": table.name, "column_name": pk.name},
        ).scalar_one_or_none()
        if not sequence:
            continue
        max_id = conn.execute(select(func.max(pk))).scalar_one()
        if max_id is None:
            conn.execute(text("SELECT setval(:sequence_name, 1, false)"), {"sequence_name": sequence})
        else:
            conn.execute(text("SELECT setval(:sequence_name, :value, true)"), {"sequence_name": sequence, "value": int(max_id)})

