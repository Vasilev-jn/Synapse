from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .monitor import NewListingRecord
from .profit_estimator import estimate_profit, extract_delivery_price_rub


SCHEMA_VERSION = 1


@dataclass
class ImportStats:
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    metrics: int = 0

    def __add__(self, other: "ImportStats") -> "ImportStats":
        return ImportStats(
            inserted=self.inserted + other.inserted,
            updated=self.updated + other.updated,
            skipped=self.skipped + other.skipped,
            metrics=self.metrics + other.metrics,
        )


CREATE_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS listings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        canonical_key TEXT NOT NULL UNIQUE,
        source TEXT NOT NULL,
        source_file TEXT,
        external_id TEXT,
        first_seen_at TEXT,
        scraped_at TEXT,
        posted_at TEXT,
        title TEXT,
        price_rub INTEGER,
        currency TEXT,
        seller_city TEXT,
        address TEXT,
        delivery_text TEXT,
        delivery_available TEXT,
        delivery_price_rub INTEGER,
        description TEXT,
        url TEXT,
        status TEXT,
        seller_name TEXT,
        seller_type TEXT,
        raw_card_texts_json TEXT,
        raw_detail_texts_json TEXT,
        raw_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS listing_metrics (
        listing_id INTEGER PRIMARY KEY,
        item_kind TEXT,
        lot_type TEXT,
        listing_type TEXT,
        normalized_title TEXT,
        matched_games_json TEXT NOT NULL DEFAULT '[]',
        extracted_game_prices_json TEXT NOT NULL DEFAULT '[]',
        disc_count INTEGER,
        game_resale_total_rub INTEGER,
        game_purchase_basis_total_rub INTEGER,
        controller_count INTEGER,
        original_controller_count INTEGER,
        third_party_controller_count INTEGER,
        controller_value_rub INTEGER,
        console_model TEXT,
        console_storage_gb INTEGER,
        condition_flags_json TEXT NOT NULL DEFAULT '{}',
        critical_defects_json TEXT NOT NULL DEFAULT '[]',
        condition_score REAL,
        profit_estimate_json TEXT NOT NULL DEFAULT '{}',
        expected_profit_rub INTEGER,
        margin_ratio REAL,
        lot_games_count INTEGER,
        lot_known_games_count INTEGER,
        lot_unknown_games_count INTEGER,
        lot_quick_sell_sum INTEGER,
        lot_sale_commission_amount REAL,
        lot_fixed_cost INTEGER,
        lot_expected_profit REAL,
        lot_profit_decision TEXT,
        lot_reasons_json TEXT NOT NULL DEFAULT '[]',
        lot_risks_json TEXT NOT NULL DEFAULT '[]',
        lot_items_json TEXT NOT NULL DEFAULT '[]',
        is_interesting TEXT,
        interest_reason TEXT,
        llm_analysis_json TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT NOT NULL,
        FOREIGN KEY(listing_id) REFERENCES listings(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS game_catalog (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        resale_price_rub INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS game_aliases (
        game_id INTEGER NOT NULL,
        alias TEXT NOT NULL,
        normalized_alias TEXT NOT NULL,
        UNIQUE(game_id, normalized_alias),
        FOREIGN KEY(game_id) REFERENCES game_catalog(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_listings_url ON listings(url)",
    "CREATE INDEX IF NOT EXISTS idx_listings_price ON listings(price_rub)",
    "CREATE INDEX IF NOT EXISTS idx_metrics_kind ON listing_metrics(item_kind, lot_type)",
    "CREATE INDEX IF NOT EXISTS idx_metrics_interesting ON listing_metrics(is_interesting)",
]


def build_database(
    *,
    db_path: Path,
    current_jsonl_path: Path,
    vidachi_dir: Path,
    price_map: dict[str, Any],
    reset: bool = False,
    extra_current_jsonl_paths: list[Path] | None = None,
) -> ImportStats:
    if reset and db_path.exists():
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    stats = ImportStats()
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        ensure_schema(conn)
        import_game_catalog(conn, price_map)
        current_paths = [current_jsonl_path, *(extra_current_jsonl_paths or [])]
        for current_path in current_paths:
            if current_path.exists():
                stats += import_current_jsonl(conn, current_path, price_map)
        if vidachi_dir.exists():
            for path in sorted(vidachi_dir.glob("*.json")):
                stats += import_vidachi_json(conn, path, price_map)
        conn.commit()
    return stats


def upsert_phone_record(
    *,
    db_path: Path,
    raw: dict[str, Any],
    source_file: str,
    price_map: dict[str, Any],
) -> ImportStats:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        ensure_schema(conn)
        import_game_catalog(conn, price_map)
        record = listing_from_current(raw, source_file=source_file)
        stats = upsert_listing_with_metrics(conn, record, price_map)
        conn.commit()
        return stats


def ensure_schema(conn: sqlite3.Connection) -> None:
    for statement in CREATE_STATEMENTS:
        conn.execute(statement)
    ensure_listing_metrics_columns(conn)
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        ("schema_version", str(SCHEMA_VERSION)),
    )


def ensure_listing_metrics_columns(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(listing_metrics)").fetchall()}
    additions = {
        "listing_type": "TEXT",
        "lot_games_count": "INTEGER",
        "lot_known_games_count": "INTEGER",
        "lot_unknown_games_count": "INTEGER",
        "lot_quick_sell_sum": "INTEGER",
        "lot_sale_commission_amount": "REAL",
        "lot_fixed_cost": "INTEGER",
        "lot_expected_profit": "REAL",
        "lot_profit_decision": "TEXT",
        "lot_reasons_json": "TEXT NOT NULL DEFAULT '[]'",
        "lot_risks_json": "TEXT NOT NULL DEFAULT '[]'",
        "lot_items_json": "TEXT NOT NULL DEFAULT '[]'",
    }
    for name, definition in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE listing_metrics ADD COLUMN {name} {definition}")


def import_game_catalog(conn: sqlite3.Connection, price_map: dict[str, Any]) -> None:
    games = price_map.get("games", [])
    if not isinstance(games, list):
        return
    for game in games:
        if not isinstance(game, dict):
            continue
        name = str(game.get("name") or "").strip()
        price = int(game.get("price") or 0)
        if not name or not price:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO game_catalog(name, resale_price_rub) VALUES (?, ?)",
            (name, price),
        )
        game_id = conn.execute("SELECT id FROM game_catalog WHERE name = ?", (name,)).fetchone()[0]
        aliases = game.get("aliases", [])
        if not isinstance(aliases, list):
            aliases = []
        for alias in [name, *aliases]:
            alias_text = str(alias).strip()
            normalized = normalize_metric_text(alias_text)
            if alias_text and normalized:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO game_aliases(game_id, alias, normalized_alias)
                    VALUES (?, ?, ?)
                    """,
                    (game_id, alias_text, normalized),
                )


def import_current_jsonl(conn: sqlite3.Connection, path: Path, price_map: dict[str, Any]) -> ImportStats:
    stats = ImportStats()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            stats.skipped += 1
            continue
        record = listing_from_current(raw, source_file=str(path))
        stats += upsert_listing_with_metrics(conn, record, price_map)
    return stats


def import_vidachi_json(conn: sqlite3.Connection, path: Path, price_map: dict[str, Any]) -> ImportStats:
    stats = ImportStats()
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        stats.skipped += 1
        return stats
    if not isinstance(payload, list):
        stats.skipped += 1
        return stats
    for raw in payload:
        if not isinstance(raw, dict):
            stats.skipped += 1
            continue
        record = listing_from_vidachi(raw, source_file=str(path))
        stats += upsert_listing_with_metrics(conn, record, price_map)
    return stats


def upsert_listing_with_metrics(
    conn: sqlite3.Connection,
    record: dict[str, Any],
    price_map: dict[str, Any],
) -> ImportStats:
    stats = ImportStats()
    key = record.get("canonical_key")
    if not key:
        stats.skipped += 1
        return stats
    now = datetime.now().isoformat(timespec="seconds")
    existing = conn.execute("SELECT id FROM listings WHERE canonical_key = ?", (key,)).fetchone()
    params = {
        **record,
        "raw_json": json.dumps(record.get("raw") or {}, ensure_ascii=False),
        "raw_card_texts_json": json.dumps(record.get("raw_card_texts") or [], ensure_ascii=False),
        "raw_detail_texts_json": json.dumps(record.get("raw_detail_texts") or [], ensure_ascii=False),
        "created_at": now,
        "updated_at": now,
    }
    if existing:
        listing_id = int(existing[0])
        conn.execute(
            """
            UPDATE listings SET
                source = COALESCE(:source, source),
                source_file = COALESCE(:source_file, source_file),
                external_id = COALESCE(:external_id, external_id),
                first_seen_at = COALESCE(first_seen_at, :first_seen_at),
                scraped_at = COALESCE(:scraped_at, scraped_at),
                posted_at = COALESCE(:posted_at, posted_at),
                title = COALESCE(:title, title),
                price_rub = COALESCE(:price_rub, price_rub),
                currency = COALESCE(:currency, currency),
                seller_city = COALESCE(:seller_city, seller_city),
                address = COALESCE(:address, address),
                delivery_text = COALESCE(:delivery_text, delivery_text),
                delivery_available = COALESCE(:delivery_available, delivery_available),
                delivery_price_rub = COALESCE(:delivery_price_rub, delivery_price_rub),
                description = COALESCE(:description, description),
                url = COALESCE(:url, url),
                status = COALESCE(:status, status),
                seller_name = COALESCE(:seller_name, seller_name),
                seller_type = COALESCE(:seller_type, seller_type),
                raw_card_texts_json = :raw_card_texts_json,
                raw_detail_texts_json = :raw_detail_texts_json,
                raw_json = :raw_json,
                updated_at = :updated_at
            WHERE id = :id
            """,
            {**params, "id": listing_id},
        )
        stats.updated += 1
    else:
        cursor = conn.execute(
            """
            INSERT INTO listings(
                canonical_key, source, source_file, external_id, first_seen_at, scraped_at, posted_at,
                title, price_rub, currency, seller_city, address, delivery_text, delivery_available,
                delivery_price_rub, description, url, status, seller_name, seller_type,
                raw_card_texts_json, raw_detail_texts_json, raw_json, created_at, updated_at
            )
            VALUES (
                :canonical_key, :source, :source_file, :external_id, :first_seen_at, :scraped_at, :posted_at,
                :title, :price_rub, :currency, :seller_city, :address, :delivery_text, :delivery_available,
                :delivery_price_rub, :description, :url, :status, :seller_name, :seller_type,
                :raw_card_texts_json, :raw_detail_texts_json, :raw_json, :created_at, :updated_at
            )
            """,
            params,
        )
        listing_id = int(cursor.lastrowid)
        stats.inserted += 1

    metric = build_metric_record(record, price_map)
    conn.execute(
        """
        INSERT OR REPLACE INTO listing_metrics(
            listing_id, item_kind, lot_type, listing_type, normalized_title, matched_games_json,
            extracted_game_prices_json, disc_count, game_resale_total_rub,
            game_purchase_basis_total_rub, controller_count, original_controller_count,
            third_party_controller_count, controller_value_rub, console_model, console_storage_gb,
            condition_flags_json, critical_defects_json, condition_score, profit_estimate_json,
            expected_profit_rub, margin_ratio, lot_games_count, lot_known_games_count,
            lot_unknown_games_count, lot_quick_sell_sum, lot_sale_commission_amount,
            lot_fixed_cost, lot_expected_profit, lot_profit_decision, lot_reasons_json,
            lot_risks_json, lot_items_json, is_interesting, interest_reason,
            llm_analysis_json, updated_at
        )
        VALUES (
            :listing_id, :item_kind, :lot_type, :listing_type, :normalized_title, :matched_games_json,
            :extracted_game_prices_json, :disc_count, :game_resale_total_rub,
            :game_purchase_basis_total_rub, :controller_count, :original_controller_count,
            :third_party_controller_count, :controller_value_rub, :console_model, :console_storage_gb,
            :condition_flags_json, :critical_defects_json, :condition_score, :profit_estimate_json,
            :expected_profit_rub, :margin_ratio, :lot_games_count, :lot_known_games_count,
            :lot_unknown_games_count, :lot_quick_sell_sum, :lot_sale_commission_amount,
            :lot_fixed_cost, :lot_expected_profit, :lot_profit_decision, :lot_reasons_json,
            :lot_risks_json, :lot_items_json, :is_interesting, :interest_reason,
            :llm_analysis_json, :updated_at
        )
        """,
        {"listing_id": listing_id, **metric, "updated_at": now},
    )
    stats.metrics += 1
    return stats


def build_metric_record(record: dict[str, Any], price_map: dict[str, Any]) -> dict[str, Any]:
    analysis = record.get("analysis") if isinstance(record.get("analysis"), dict) else {}
    fake = NewListingRecord(
        saved_at=str(record.get("first_seen_at") or ""),
        fingerprint=str(record.get("canonical_key") or ""),
        title=record.get("title"),
        price=record.get("price_rub"),
        address=record.get("address"),
        description=record.get("description"),
        url=record.get("url"),
        raw_card_texts=record.get("raw_card_texts") or [],
        raw_detail_texts=record.get("raw_detail_texts") or [],
        delivery_text=record.get("delivery_text"),
        seller_city=record.get("seller_city"),
        item_kind=record.get("item_kind"),
        analysis=analysis or None,
    )
    profit = estimate_profit(fake, price_map)
    full_text = "\n".join(
        [
            str(record.get("title") or ""),
            str(record.get("description") or ""),
            "\n".join(str(x) for x in record.get("raw_detail_texts") or []),
        ]
    )
    controller_counts = extract_controller_counts(full_text)
    extracted_prices = extract_game_line_prices(record.get("description") or "")
    condition = heuristic_condition_flags(full_text)
    profit_interest = str(profit.get("is_interesting_by_profit") or "unclear")
    condition_score = condition["score"]
    item_kind = record.get("item_kind") or analysis.get("item_kind") or infer_item_kind(full_text)
    lot_type = metric_lot_type(item_kind, profit, extracted_prices, full_text)
    is_interesting = "yes" if profit_interest == "yes" and condition_score >= 0.8 and not condition["critical"] else "no"
    if profit_interest == "unclear":
        is_interesting = "unclear"
    if lot_type == "catalog_or_price_list":
        is_interesting = "no"
    reason = build_interest_reason(profit_interest, condition_score, condition["critical"])
    if lot_type == "catalog_or_price_list":
        reason = f"{reason}; catalog_or_price_list=true"
    return {
        "item_kind": item_kind,
        "lot_type": lot_type,
        "listing_type": profit.get("listing_type") or profit.get("scenario") or lot_type,
        "normalized_title": normalize_metric_text(record.get("title")),
        "matched_games_json": json.dumps(profit.get("matched_games") or [], ensure_ascii=False),
        "extracted_game_prices_json": json.dumps(extracted_prices, ensure_ascii=False),
        "disc_count": int(profit.get("disc_count_hint") or 0) or None,
        "game_resale_total_rub": profit.get("resale_total_rub"),
        "game_purchase_basis_total_rub": profit.get("buy_threshold_rub") or profit.get("disc_buy_value_rub"),
        "controller_count": controller_counts["total"],
        "original_controller_count": controller_counts["original"],
        "third_party_controller_count": controller_counts["third_party"],
        "controller_value_rub": controller_counts["value_rub"],
        "console_model": profit.get("console_model") or infer_console_model(full_text),
        "console_storage_gb": extract_storage_gb(full_text),
        "condition_flags_json": json.dumps(condition["flags"], ensure_ascii=False),
        "critical_defects_json": json.dumps(condition["critical"], ensure_ascii=False),
        "condition_score": condition_score,
        "profit_estimate_json": json.dumps(profit, ensure_ascii=False),
        "expected_profit_rub": profit.get("estimated_profit_rub"),
        "margin_ratio": profit.get("margin_ratio"),
        "lot_games_count": profit.get("lot_games_count"),
        "lot_known_games_count": profit.get("lot_known_games_count"),
        "lot_unknown_games_count": profit.get("lot_unknown_games_count"),
        "lot_quick_sell_sum": profit.get("lot_quick_sell_sum"),
        "lot_sale_commission_amount": profit.get("lot_sale_commission_amount"),
        "lot_fixed_cost": profit.get("lot_fixed_cost"),
        "lot_expected_profit": profit.get("lot_expected_profit"),
        "lot_profit_decision": profit.get("lot_profit_decision") or profit.get("decision"),
        "lot_reasons_json": json.dumps(profit.get("lot_reasons") or [], ensure_ascii=False),
        "lot_risks_json": json.dumps(profit.get("lot_risks") or profit.get("risks") or [], ensure_ascii=False),
        "lot_items_json": json.dumps(profit.get("lot_items") or [], ensure_ascii=False),
        "is_interesting": is_interesting,
        "interest_reason": reason,
        "llm_analysis_json": json.dumps(analysis or {}, ensure_ascii=False),
    }


def listing_from_current(raw: dict[str, Any], source_file: str) -> dict[str, Any]:
    url = clean_url(raw.get("url"))
    address = clean_address(raw.get("address"))
    seller_city = raw.get("seller_city") or city_from_address(address)
    raw_card_texts = raw.get("raw_card_texts") or []
    raw_detail_texts = raw.get("raw_detail_texts") or []
    delivery_text = usable_delivery_text(raw.get("delivery_text")) or find_delivery_text(raw_card_texts, raw_detail_texts)
    price_rub = extract_listing_price(raw_card_texts) or as_int(raw.get("price")) or extract_listing_price(raw_detail_texts)
    title = clean_listing_title(raw.get("title"), raw_card_texts)
    return {
        "canonical_key": canonical_key(url, raw.get("fingerprint")),
        "source": str(raw.get("source") or "phone_monitor"),
        "source_file": source_file,
        "external_id": raw.get("fingerprint"),
        "first_seen_at": raw.get("saved_at"),
        "scraped_at": raw.get("saved_at"),
        "posted_at": None,
        "title": title,
        "price_rub": price_rub,
        "currency": "RUB",
        "seller_city": seller_city,
        "address": address,
        "delivery_text": delivery_text,
        "delivery_available": delivery_available(raw, delivery_text),
        "delivery_price_rub": extract_delivery_price(delivery_text),
        "description": raw.get("description"),
        "url": url,
        "status": None,
        "seller_name": None,
        "seller_type": None,
        "raw_card_texts": raw_card_texts,
        "raw_detail_texts": raw_detail_texts,
        "analysis": raw.get("analysis"),
        "item_kind": raw.get("item_kind"),
        "raw": raw,
    }


def listing_from_vidachi(raw: dict[str, Any], source_file: str) -> dict[str, Any]:
    url = clean_url(raw.get("url"))
    seller = raw.get("seller") if isinstance(raw.get("seller"), dict) else {}
    delivery_text = "Avito delivery available" if raw.get("isDeliveryAvailable") is True else None
    address = clean_address(raw.get("address"))
    return {
        "canonical_key": canonical_key(url, raw.get("id")),
        "source": "vidachi_json",
        "source_file": source_file,
        "external_id": str(raw.get("id") or ""),
        "first_seen_at": raw.get("scrapedAt"),
        "scraped_at": raw.get("scrapedAt"),
        "posted_at": raw.get("postedAt"),
        "title": raw.get("title"),
        "price_rub": as_int(raw.get("price")),
        "currency": raw.get("currency") or "RUB",
        "seller_city": city_from_address(address),
        "address": address,
        "delivery_text": delivery_text,
        "delivery_available": "yes" if raw.get("isDeliveryAvailable") is True else "no",
        "delivery_price_rub": None,
        "description": raw.get("description"),
        "url": url,
        "status": raw.get("status"),
        "seller_name": seller.get("name") if seller else None,
        "seller_type": raw.get("userType") or (seller.get("type") if seller else None),
        "raw_card_texts": [],
        "raw_detail_texts": [],
        "analysis": None,
        "item_kind": None,
        "raw": raw,
    }


def clean_url(url: object) -> str | None:
    if not url:
        return None
    raw = str(url).strip()
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not k.startswith("utm_")])
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), query, ""))


def canonical_key(url: str | None, fallback: object) -> str | None:
    if url:
        return f"url:{url}"
    if fallback:
        return f"id:{fallback}"
    return None


def city_from_address(address: object) -> str | None:
    if not address:
        return None
    first = str(clean_address(address) or "").splitlines()[0].strip()
    if "," in first:
        return first.split(",")[-1].strip()
    return first or None


CTA_PREFIXES = (
    "купить",
    "в корзину",
    "договориться",
    "предложить",
    "попросить",
    "написать",
    "позвонить",
    "оформить",
    "оплата частями",
)


def clean_address(address: object) -> str | None:
    if not address:
        return None
    kept: list[str] = []
    for line in str(address).replace("\xa0", " ").splitlines():
        text = re.sub(r"\s+", " ", line).strip()
        if not text:
            continue
        lowered = text.lower().replace("ё", "е")
        if lowered.startswith(CTA_PREFIXES):
            break
        kept.append(text)
    return "\n".join(kept).strip() or None


def as_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    text = re.sub(r"[^\d]", "", str(value))
    return int(text) if text else None


BAD_TITLE_WORDS = {
    "просмотрено",
    "рассрочка",
    "поделиться",
    "избранное",
}


def looks_like_bad_title(value: object) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    lowered = text.lower().replace("ё", "е")
    if lowered in BAD_TITLE_WORDS:
        return True
    if re.fullmatch(r"\(?\d+\)?", text):
        return True
    if re.fullmatch(r"\d,\d", text):
        return True
    if as_int(text) is not None and re.search(r"(₽|руб|р\b)", text.lower()):
        return True
    if any(word in lowered for word in ("отзыв", "час назад", "день назад")):
        return True
    return False


def title_score(value: object) -> int:
    text = str(value or "").strip()
    lowered = text.lower().replace("ё", "е")
    if looks_like_bad_title(text):
        return -1000
    score = min(len(text), 120)
    if "\n" in text or len(text) > 180:
        score -= 160
    if any(signal in lowered for signal in ("ps4", "ps 4", "playstation", "диск", "игр", "gta", "call of duty")):
        score += 80
    if re.search(r"[a-zA-Zа-яА-Я]{3,}", text):
        score += 20
    return score


def clean_listing_title(current: object, *groups: object) -> str | None:
    current_text = str(current or "").strip()
    best = current_text if not looks_like_bad_title(current_text) else ""
    best_score = title_score(best)
    for group in groups:
        if not isinstance(group, list):
            continue
        for item in group:
            candidate = str(item or "").replace("\xa0", " ").strip()
            if not candidate:
                continue
            score = title_score(candidate)
            if score > best_score:
                best = candidate
                best_score = score
    return best or None


def usable_delivery_text(value: object) -> str | None:
    text = str(value or "").replace("\xa0", " ").strip()
    if not text:
        return None
    lowered = text.lower().replace("ё", "е")
    if "достав" not in lowered and "delivery" not in lowered and "отправ" not in lowered:
        return None
    if "?" in text and lowered.startswith(("отправите", "сможете отправить", "авито доставкой сможете")):
        return None
    if lowered.startswith(("здравствуйте", "готов", "интересует")):
        return None
    return text


def find_delivery_text(*groups: object) -> str | None:
    candidates: list[str] = []
    for group in groups:
        if not isinstance(group, list):
            continue
        for item in group:
            text = usable_delivery_text(item)
            if text:
                candidates.append(text)
    priced = [text for text in candidates if extract_delivery_price(text) is not None]
    if priced:
        return priced[0]
    return candidates[0] if candidates else None


def delivery_available(raw: dict[str, Any], delivery_text: str | None) -> str:
    delivery_text = usable_delivery_text(delivery_text)
    if delivery_text:
        lowered = delivery_text.lower()
        if "отключена" in lowered or "нет достав" in lowered:
            return "no"
        return "yes"
    if raw.get("delivery_available") in ("yes", "no", "unclear"):
        return str(raw["delivery_available"])
    return "unclear"


def extract_delivery_price(text: str | None) -> int | None:
    return extract_delivery_price_rub(text)


def extract_listing_price(*groups: object) -> int | None:
    candidates: list[int] = []
    for group in groups:
        if not isinstance(group, list):
            continue
        for item in group:
            text = str(item or "").replace("\xa0", " ").strip()
            if not text:
                continue
            lowered = text.lower().replace("ё", "е")
            if any(skip in lowered for skip in ("достав", "рассроч", "оплата частями", "скидк", "бонус")):
                continue
            match = re.search(r"(?<!\d)(\d[\d\s]{1,8})\s*(?:₽|руб\.?|р\.?)(?!\w)", text, re.IGNORECASE)
            if not match:
                continue
            value = as_int(match.group(1))
            if value is not None and 50 <= value <= 500000:
                candidates.append(value)
    return candidates[0] if candidates else None


def normalize_metric_text(value: object) -> str:
    text = str(value or "").lower().replace("ё", "е")
    text = re.sub(r"[^0-9a-zа-я]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_game_line_prices(description: str) -> list[dict[str, object]]:
    prices: list[dict[str, object]] = []
    for line in description.splitlines():
        normalized = line.strip()
        if len(normalized) < 4:
            continue
        match = re.search(r"(.{2,80}?)(?:\s+[-–—:]\s+|\s+)(\d[\d\s]{2,5})\s*(?:₽|руб|р\b)", normalized, re.IGNORECASE)
        if match:
            price = as_int(match.group(2))
            if price:
                prices.append({"name_text": match.group(1).strip(" -–—:"), "price_rub": price, "source_line": normalized})
    return prices


def extract_controller_counts(text: str) -> dict[str, int]:
    normalized = normalize_metric_text(text)
    count = 0
    match = re.search(r"\b(\d{1,2})\s*(?:джойстик|джоя|геймпад|контроллер)", normalized)
    if match:
        count = int(match.group(1))
    elif re.search(r"\b(два|2)\s*(?:джойстик|джоя|геймпад|контроллер)", normalized):
        count = 2
    elif any(word in normalized for word in ("джойстик", "геймпад", "контроллер", "dualshock")):
        count = 1
    original = count if any(word in normalized for word in ("оригинал", "dualshock", "dualsense")) else 0
    third_party = count if any(word in normalized for word in ("не оригинал", "копия", "аналог", "паль")) else 0
    if original and third_party:
        original = max(0, count - third_party)
    value = original * 1100 + third_party * 700
    if count and not value:
        value = count * 1000
    return {"total": count, "original": original, "third_party": third_party, "value_rub": value}


def infer_console_model(text: str) -> str | None:
    normalized = normalize_metric_text(text)
    if "pro" in normalized or "пс4 про" in normalized:
        return "pro"
    if "slim" in normalized:
        return "slim"
    if "fat" in normalized:
        return "fat"
    if looks_like_console_hardware(normalized):
        return "fat"
    return None


def extract_storage_gb(text: str) -> int | None:
    normalized = normalize_metric_text(text)
    if re.search(r"\b1\s*(?:tb|тб)\b", normalized):
        return 1000
    match = re.search(r"\b(500|1000)\s*(?:gb|гб)\b", normalized)
    return int(match.group(1)) if match else None


def infer_item_kind(text: str) -> str:
    normalized = normalize_metric_text(text)
    if looks_like_console_hardware(normalized):
        return "console"
    if any(word in normalized for word in ("диск", "игра", "ps4")):
        return "game"
    return "unknown"


def looks_like_console_hardware(normalized: str) -> bool:
    return any(
        word in normalized
        for word in (
            "ps4 slim",
            "ps4 pro",
            "ps4 fat",
            "ps4 phat",
            "ps 4 slim",
            "ps 4 pro",
            "ps 4 fat",
            "ps 4 phat",
            "playstation 4 slim",
            "playstation 4 pro",
            "playstation 4 fat",
            "playstation 4 phat",
            "игровая приставка",
            "приставка playstation",
            "приставка ps4",
            "консоль playstation",
            "консоль ps4",
            "500 gb",
            "500гб",
            "500 гб",
            "1 tb",
            "1tb",
            "1 тб",
            "dualshock",
        )
    )


def metric_lot_type(item_kind: object, profit: dict[str, object], extracted_prices: list[dict[str, object]], text: str) -> str:
    normalized = normalize_metric_text(text)
    matched_count = int(profit.get("matched_count") or 0)
    scenario = str(profit.get("scenario") or "unknown")
    if scenario == "excluded_service_or_rental":
        return scenario
    catalog_hint = any(word in normalized for word in ("обновлено", "обмен", "прайс", "ассортимент"))
    if len(extracted_prices) >= 5 or matched_count >= 10 or (catalog_hint and matched_count >= 4):
        return "catalog_or_price_list"
    if item_kind == "console" and scenario == "single_game":
        return "console_only"
    if scenario:
        return scenario
    return "unknown"


def heuristic_condition_flags(text: str) -> dict[str, object]:
    normalized = normalize_metric_text(text)
    flags = {
        "excellent": any(word in normalized for word in ("отличн", "идеальн", "без царап", "не шумит", "не перегрев")),
        "scratches": any(word in normalized for word in ("царап", "потерт", "потертост")),
        "box_damage": any(word in normalized for word in ("короб с трещ", "коробка трещ", "без облож")),
        "repair_history": any(word in normalized for word in ("ремонт", "чинил", "после ремонта", "вскрывал")),
        "broken_or_for_parts": any(word in normalized for word in ("на ремонт", "под ремонт", "под замену", "не работает", "не включается")),
        "disc_drive_issue": any(word in normalized for word in ("не читает диск", "привод шумит", "дисковод", "не читает диски")),
        "overheat_issue": any(word in normalized for word in ("перегрев", "греется", "шумит как")),
    }
    critical = [key for key in ("broken_or_for_parts", "disc_drive_issue", "overheat_issue") if flags[key]]
    penalties = {
        "scratches": 0.08,
        "box_damage": 0.04,
        "repair_history": 0.2,
        "broken_or_for_parts": 0.55,
        "disc_drive_issue": 0.35,
        "overheat_issue": 0.35,
    }
    score = 1.0
    for key, penalty in penalties.items():
        if flags[key]:
            score -= penalty
    return {"flags": flags, "critical": critical, "score": round(max(0.0, min(1.0, score)), 3)}


def build_interest_reason(profit_interest: str, condition_score: float, critical: list[str]) -> str:
    parts = [f"profit={profit_interest}", f"condition_score={condition_score:.2f}"]
    if critical:
        parts.append("critical=" + ",".join(critical))
    return "; ".join(parts)


def count_rows(db_path: Path) -> dict[str, int]:
    with sqlite3.connect(db_path) as conn:
        result = {}
        for table in ("listings", "listing_metrics", "game_catalog", "game_aliases"):
            result[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        return result
