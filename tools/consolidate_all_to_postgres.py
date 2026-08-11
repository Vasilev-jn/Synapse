from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb


ROOT = Path(__file__).resolve().parents[1]

DB_CONNINFO = os.environ.get(
    "CRM_POSTGRES_DSN",
    "postgresql://postgres:postgres@127.0.0.1:5432/crm_inventory",
)

TEXT_EXTENSIONS = {".json", ".jsonl", ".txt", ".csv", ".md", ".html", ".log"}
SQLITE_EXTENSIONS = {".db", ".sqlite", ".sqlite3"}
SCAN_ROOTS = [
    ROOT / "data",
    ROOT / "json_responses",
    ROOT / "reports",
]
ROOT_FILE_EXTENSIONS = {".zip", ".log", ".txt", ".md", ".json"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_text(path: Path) -> str:
    for encoding in ("utf-8", "utf-8-sig", "cp1251"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def parse_json_or_none(text: str) -> Any | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def ensure_schema(conn: psycopg.Connection) -> None:
    conn.execute("CREATE SCHEMA IF NOT EXISTS avito_archive")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS avito_archive.source_files (
            id bigserial PRIMARY KEY,
            source_path text NOT NULL UNIQUE,
            extension text NOT NULL,
            size_bytes bigint NOT NULL,
            mtime timestamptz NOT NULL,
            sha256 text NOT NULL,
            imported_at timestamptz NOT NULL,
            import_kind text NOT NULL,
            records_count integer NOT NULL DEFAULT 0,
            notes text
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS avito_archive.json_documents (
            source_file_id bigint PRIMARY KEY REFERENCES avito_archive.source_files(id) ON DELETE CASCADE,
            document jsonb,
            raw_text text
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS avito_archive.jsonl_records (
            source_file_id bigint NOT NULL REFERENCES avito_archive.source_files(id) ON DELETE CASCADE,
            line_no integer NOT NULL,
            record jsonb,
            raw_text text,
            PRIMARY KEY (source_file_id, line_no)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS avito_archive.csv_records (
            source_file_id bigint NOT NULL REFERENCES avito_archive.source_files(id) ON DELETE CASCADE,
            row_no integer NOT NULL,
            record jsonb NOT NULL,
            PRIMARY KEY (source_file_id, row_no)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS avito_archive.text_documents (
            source_file_id bigint PRIMARY KEY REFERENCES avito_archive.source_files(id) ON DELETE CASCADE,
            content text NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS avito_archive.sqlite_tables (
            source_file_id bigint NOT NULL REFERENCES avito_archive.source_files(id) ON DELETE CASCADE,
            table_name text NOT NULL,
            row_no integer NOT NULL,
            row_data jsonb NOT NULL,
            PRIMARY KEY (source_file_id, table_name, row_no)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS avito_archive.file_artifacts (
            source_file_id bigint PRIMARY KEY REFERENCES avito_archive.source_files(id) ON DELETE CASCADE,
            artifact_type text NOT NULL,
            original_path text NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_archive_source_files_sha
        ON avito_archive.source_files (sha256)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_archive_jsonl_records_gin
        ON avito_archive.jsonl_records USING gin (record)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_archive_json_documents_gin
        ON avito_archive.json_documents USING gin (document)
        """
    )


def upsert_source_file(conn: psycopg.Connection, path: Path, import_kind: str, records_count: int = 0, notes: str | None = None) -> int:
    stat = path.stat()
    row = conn.execute(
        """
        INSERT INTO avito_archive.source_files
            (source_path, extension, size_bytes, mtime, sha256, imported_at, import_kind, records_count, notes)
        VALUES (%s, %s, %s, to_timestamp(%s), %s, %s, %s, %s, %s)
        ON CONFLICT (source_path) DO UPDATE SET
            extension = EXCLUDED.extension,
            size_bytes = EXCLUDED.size_bytes,
            mtime = EXCLUDED.mtime,
            sha256 = EXCLUDED.sha256,
            imported_at = EXCLUDED.imported_at,
            import_kind = EXCLUDED.import_kind,
            records_count = EXCLUDED.records_count,
            notes = EXCLUDED.notes
        RETURNING id
        """,
        (
            rel(path),
            path.suffix.lower(),
            stat.st_size,
            stat.st_mtime,
            sha256_file(path),
            utcnow(),
            import_kind,
            records_count,
            notes,
        ),
    ).fetchone()
    assert row is not None
    return int(row[0])


def clear_file_children(conn: psycopg.Connection, source_file_id: int) -> None:
    for table in (
        "json_documents",
        "jsonl_records",
        "csv_records",
        "text_documents",
        "sqlite_tables",
        "file_artifacts",
    ):
        conn.execute(f"DELETE FROM avito_archive.{table} WHERE source_file_id = %s", (source_file_id,))


def import_json(conn: psycopg.Connection, path: Path) -> int:
    text = read_text(path)
    document = parse_json_or_none(text)
    source_id = upsert_source_file(conn, path, "json_document", 1 if document is not None else 0)
    clear_file_children(conn, source_id)
    conn.execute(
        "INSERT INTO avito_archive.json_documents (source_file_id, document, raw_text) VALUES (%s, %s, %s)",
        (source_id, Jsonb(document) if document is not None else None, text if document is None else None),
    )
    return 1


def import_jsonl(conn: psycopg.Connection, path: Path) -> int:
    text = read_text(path)
    rows: list[tuple[int, Any | None, str | None]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        parsed = parse_json_or_none(line)
        rows.append((line_no, parsed, None if parsed is not None else line))
    source_id = upsert_source_file(conn, path, "jsonl_records", len(rows))
    clear_file_children(conn, source_id)
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO avito_archive.jsonl_records (source_file_id, line_no, record, raw_text) VALUES (%s, %s, %s, %s)",
            [(source_id, line_no, Jsonb(parsed) if parsed is not None else None, raw) for line_no, parsed, raw in rows],
        )
    return len(rows)


def import_csv(conn: psycopg.Connection, path: Path) -> int:
    text = read_text(path)
    reader = csv.DictReader(text.splitlines())
    records = list(reader)
    source_id = upsert_source_file(conn, path, "csv_records", len(records))
    clear_file_children(conn, source_id)
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO avito_archive.csv_records (source_file_id, row_no, record) VALUES (%s, %s, %s)",
            [(source_id, index, Jsonb(record)) for index, record in enumerate(records, start=1)],
        )
    return len(records)


def import_text(conn: psycopg.Connection, path: Path, kind: str) -> int:
    text = read_text(path)
    source_id = upsert_source_file(conn, path, kind, 1)
    clear_file_children(conn, source_id)
    conn.execute(
        "INSERT INTO avito_archive.text_documents (source_file_id, content) VALUES (%s, %s)",
        (source_id, text),
    )
    return 1


def import_sqlite(conn: psycopg.Connection, path: Path) -> int:
    source_id = upsert_source_file(conn, path, "sqlite_database", 0)
    clear_file_children(conn, source_id)
    total = 0
    src = sqlite3.connect(path)
    src.row_factory = sqlite3.Row
    try:
        tables = [
            row[0]
            for row in src.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        with conn.cursor() as cur:
            for table in tables:
                rows = src.execute(f'SELECT * FROM "{table}"').fetchall()
                batch = []
                for index, row in enumerate(rows, start=1):
                    batch.append((source_id, table, index, Jsonb(dict(row))))
                if batch:
                    cur.executemany(
                        """
                        INSERT INTO avito_archive.sqlite_tables (source_file_id, table_name, row_no, row_data)
                        VALUES (%s, %s, %s, %s)
                        """,
                        batch,
                    )
                    total += len(batch)
    finally:
        src.close()
    conn.execute("UPDATE avito_archive.source_files SET records_count = %s WHERE id = %s", (total, source_id))
    return total


def import_artifact(conn: psycopg.Connection, path: Path, artifact_type: str) -> int:
    source_id = upsert_source_file(conn, path, "file_artifact", 1, notes="metadata only; binary content kept on disk")
    clear_file_children(conn, source_id)
    conn.execute(
        "INSERT INTO avito_archive.file_artifacts (source_file_id, artifact_type, original_path) VALUES (%s, %s, %s)",
        (source_id, artifact_type, str(path.resolve())),
    )
    return 1


def iter_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.iterdir():
        if path.is_file() and path.name.lower() != ".env" and path.suffix.lower() in ROOT_FILE_EXTENSIONS:
            files.append(path)
    for root in SCAN_ROOTS:
        if root.exists():
            files.extend(path for path in root.rglob("*") if path.is_file())
    return sorted(set(files))


def main() -> int:
    stats: dict[str, int] = {
        "files_seen": 0,
        "files_imported": 0,
        "records_imported": 0,
        "errors": 0,
    }
    with psycopg.connect(DB_CONNINFO) as conn:
        ensure_schema(conn)
        for path in iter_files():
            stats["files_seen"] += 1
            ext = path.suffix.lower()
            try:
                if ext == ".json":
                    count = import_json(conn, path)
                elif ext == ".jsonl":
                    count = import_jsonl(conn, path)
                elif ext == ".csv":
                    count = import_csv(conn, path)
                elif ext in {".txt", ".md", ".log"}:
                    count = import_text(conn, path, f"{ext[1:]}_document")
                elif ext == ".html":
                    count = import_text(conn, path, "html_snapshot")
                elif ext in SQLITE_EXTENSIONS:
                    count = import_sqlite(conn, path)
                else:
                    count = import_artifact(conn, path, ext.lstrip(".") or "unknown")
                stats["files_imported"] += 1
                stats["records_imported"] += count
                if stats["files_imported"] % 100 == 0:
                    conn.commit()
                    print(json.dumps(stats, ensure_ascii=False), flush=True)
            except Exception as exc:
                stats["errors"] += 1
                print(f"[ERROR] {rel(path)}: {exc}", flush=True)
        conn.commit()
    print(json.dumps(stats, ensure_ascii=False, indent=2), flush=True)
    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
