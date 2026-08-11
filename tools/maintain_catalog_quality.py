from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg


DSN = "postgresql://postgres:postgres@127.0.0.1:5432/crm_inventory"

GAME_CATEGORY_ID = 2
CONSOLE_CATEGORY_ID = 3
ACCESSORY_CATEGORY_ID = 4


@dataclass(frozen=True)
class Canonical:
    category_id: int
    name: str
    aliases: tuple[str, ...]


CANONICALS: tuple[Canonical, ...] = (
    Canonical(CONSOLE_CATEGORY_ID, "PlayStation 4 Fat 500GB", (
        "PS4 Fat 500GB", "PS 4 Fat 500GB", "Sony PlayStation 4 Fat 500 GB",
        "PlayStation 4 Fat 500 GB", "PS4 CUH-10", "PS4 CUH-11", "PS4 CUH-12",
        "CUH-1008", "CUH-1108", "CUH-1208",
    )),
    Canonical(CONSOLE_CATEGORY_ID, "PlayStation 4 Fat 1TB", (
        "PS4 Fat 1TB", "PS 4 Fat 1TB", "Sony PlayStation 4 Fat 1 TB",
        "PlayStation 4 Fat 1 TB", "CUH-1208B",
    )),
    Canonical(CONSOLE_CATEGORY_ID, "PlayStation 4 Slim 500GB", (
        "PS4 Slim 500GB", "PS 4 Slim 500GB", "Sony PlayStation 4 Slim 500 GB",
        "PlayStation 4 Slim 500 GB", "PS4 CUH-20", "PS4 CUH-21", "PS4 CUH-22",
        "CUH-2008", "CUH-2108", "CUH-2208",
    )),
    Canonical(CONSOLE_CATEGORY_ID, "PlayStation 4 Slim 1TB", (
        "PS4 Slim 1TB", "PS 4 Slim 1TB", "Sony PlayStation 4 Slim 1 TB",
        "PlayStation 4 Slim 1 TB", "CUH-2016B", "CUH-2216B",
    )),
    Canonical(CONSOLE_CATEGORY_ID, "PlayStation 4 Pro 1TB", (
        "PS4 Pro", "PS4 Pro 1TB", "PS 4 Pro", "Sony PlayStation 4 Pro",
        "Sony PlayStation 4 Pro 1 TB", "PS4 CUH-70", "PS4 CUH-71", "PS4 CUH-72",
        "CUH-7008", "CUH-7108", "CUH-7208",
    )),
    Canonical(CONSOLE_CATEGORY_ID, "PlayStation 5 Disc", (
        "PS5 Disc", "PS5 с дисководом", "PlayStation 5 с дисководом",
        "Sony PlayStation 5 Disc Edition", "PlayStation 5 Fat Disc",
    )),
    Canonical(CONSOLE_CATEGORY_ID, "PlayStation 5 Digital", (
        "PS5 Digital", "PS5 Digital Edition", "PlayStation 5 Digital Edition",
        "Sony PlayStation 5 Digital Edition", "PS5 без дисковода",
    )),
    Canonical(CONSOLE_CATEGORY_ID, "PlayStation 5 Slim Disc", (
        "PS5 Slim Disc", "PlayStation 5 Slim с дисководом", "Sony PlayStation 5 Slim Disc",
    )),
    Canonical(CONSOLE_CATEGORY_ID, "PlayStation 5 Slim Digital", (
        "PS5 Slim Digital", "PlayStation 5 Slim Digital Edition", "PS5 Slim без дисковода",
    )),
    Canonical(ACCESSORY_CATEGORY_ID, "DualShock 4", (
        "DualShock 4", "DS4", "Геймпад PS4", "Джойстик PS4", "Джостик PS4",
        "Контроллер PS4", "Оригинальный геймпад PS4", "Sony DualShock 4",
    )),
    Canonical(ACCESSORY_CATEGORY_ID, "DualSense", (
        "DualSense", "DualSense Controller", "Геймпад PS5", "Джойстик PS5",
        "Контроллер PS5", "Sony DualSense", "DualSense Wireless Controller",
    )),
    Canonical(ACCESSORY_CATEGORY_ID, "Зарядная станция DualShock 4", (
        "Зарядка DualShock 4", "Зарядная станция PS4", "Док-станция PS4",
        "Charging Station DualShock 4",
    )),
    Canonical(ACCESSORY_CATEGORY_ID, "Зарядная станция DualSense", (
        "Зарядка DualSense", "Зарядная станция PS5", "Док-станция PS5",
        "Charging Station DualSense",
    )),
    Canonical(ACCESSORY_CATEGORY_ID, "PlayStation Camera", (
        "PS Camera", "Камера PS4", "PlayStation Camera PS4", "Sony PS Camera",
    )),
    Canonical(ACCESSORY_CATEGORY_ID, "PlayStation VR", (
        "PS VR", "PSVR", "PlayStation VR шлем", "VR PlayStation",
    )),
    Canonical(ACCESSORY_CATEGORY_ID, "PlayStation Portal", (
        "PS Portal", "Sony PlayStation Portal", "PlayStation 5 Portal",
    )),
)


MERGES: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (GAME_CATEGORY_ID, "Call of Duty: Black Ops III", ("Call of Duty Black Ops 3",)),
    (GAME_CATEGORY_ID, "Sniper Elite III", ("Sniper Elite 3",)),
    (GAME_CATEGORY_ID, "Diablo III: Reaper of Souls - Ultimate Evil Edition", ("Diablo III: Reaper of Souls",)),
    (GAME_CATEGORY_ID, "Detroit Become Human", ("Detroit стать: Стать человеком",)),
    (GAME_CATEGORY_ID, "LEGO Jurassic World", ("Lego Мир Юрского Периода",)),
    (GAME_CATEGORY_ID, "Rainbow Six Siege", ("Rainbowsix Осада",)),
    (GAME_CATEGORY_ID, "The Last of Us Remastered", ("Одни из нас",)),
    (GAME_CATEGORY_ID, "Assassin's Creed Shadows Special Edition", ("Аssаssin`S Сrееd Shadows",)),
)


def norm(value: str) -> str:
    text = value.casefold().replace("ё", "е")
    text = re.sub(r"[^0-9a-zа-я]+", " ", text)
    return " ".join(text.split())


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def ensure_entry(conn, category_id: int, name: str) -> int:
    row = conn.execute(
        "select id from catalog_entries where category_id=%s and lower(name)=lower(%s)",
        (category_id, name),
    ).fetchone()
    if row:
        return int(row[0])
    row = conn.execute(
        """
        insert into catalog_entries(category_id, name, is_system, created_at, updated_at)
        values (%s, %s, true, %s, %s)
        returning id
        """,
        (category_id, name, now(), now()),
    ).fetchone()
    assert row
    return int(row[0])


def ensure_alias(conn, entry_id: int, alias: str) -> None:
    if not alias.strip():
        return
    row = conn.execute(
        "select id from catalog_aliases where catalog_entry_id=%s and lower(name)=lower(%s)",
        (entry_id, alias),
    ).fetchone()
    if row:
        return
    conn.execute(
        "insert into catalog_aliases(catalog_entry_id, name, created_at) values (%s, %s, %s) on conflict do nothing",
        (entry_id, alias, now()),
    )


def merge_entry(conn, category_id: int, target_name: str, source_names: tuple[str, ...]) -> None:
    target_id = ensure_entry(conn, category_id, target_name)
    for source_name in source_names:
        source = conn.execute(
            "select id, name from catalog_entries where category_id=%s and lower(name)=lower(%s)",
            (category_id, source_name),
        ).fetchone()
        ensure_alias(conn, target_id, source_name)
        if not source:
            continue
        source_id = int(source[0])
        if source_id == target_id:
            continue
        for table in ("items", "market_listing_matches", "price_observations"):
            conn.execute(
                f"update {table} set catalog_entry_id=%s where catalog_entry_id=%s",
                (target_id, source_id),
            )
        aliases = conn.execute("select name from catalog_aliases where catalog_entry_id=%s", (source_id,)).fetchall()
        for (alias,) in aliases:
            ensure_alias(conn, target_id, alias)
        conn.execute("delete from catalog_aliases where catalog_entry_id=%s", (source_id,))
        conn.execute("delete from catalog_entries where id=%s", (source_id,))


def classify_console_catalog_id(conn, title: str) -> int | None:
    t = norm(title)
    if not any(x in t for x in ("ps4", "ps 4", "playstation 4", "пс4", "пс 4")):
        if any(x in t for x in ("ps5", "ps 5", "playstation 5", "пс5", "пс 5")):
            if "slim" in t and ("digital" in t or "без дисковод" in t):
                return ensure_entry(conn, CONSOLE_CATEGORY_ID, "PlayStation 5 Slim Digital")
            if "slim" in t:
                return ensure_entry(conn, CONSOLE_CATEGORY_ID, "PlayStation 5 Slim Disc")
            if "digital" in t or "без дисковод" in t:
                return ensure_entry(conn, CONSOLE_CATEGORY_ID, "PlayStation 5 Digital")
            return ensure_entry(conn, CONSOLE_CATEGORY_ID, "PlayStation 5 Disc")
        return None
    storage_1tb = any(x in t for x in ("1tb", "1 tb", "1тб", "1 тб", "1000gb"))
    if "pro" in t or re.search(r"cuh ?7[0-9]", t):
        return ensure_entry(conn, CONSOLE_CATEGORY_ID, "PlayStation 4 Pro 1TB")
    if "slim" in t or re.search(r"cuh ?2[0-9]", t):
        return ensure_entry(conn, CONSOLE_CATEGORY_ID, "PlayStation 4 Slim 1TB" if storage_1tb else "PlayStation 4 Slim 500GB")
    if "fat" in t or re.search(r"cuh ?1[0-9]", t):
        return ensure_entry(conn, CONSOLE_CATEGORY_ID, "PlayStation 4 Fat 1TB" if storage_1tb else "PlayStation 4 Fat 500GB")
    return ensure_entry(conn, CONSOLE_CATEGORY_ID, "PlayStation 4 Fat 1TB" if storage_1tb else "PlayStation 4 Fat 500GB")


def classify_accessory_catalog_id(conn, title: str) -> int | None:
    t = norm(title)
    if any(x in t for x in ("dualsense", "dual sense", "геймпад ps5", "джойстик ps5", "контроллер ps5")):
        return ensure_entry(conn, ACCESSORY_CATEGORY_ID, "DualSense")
    if any(x in t for x in ("dualshock", "dual shock", "геймпад ps4", "джойстик ps4", "джостик ps4", "контроллер ps4")):
        return ensure_entry(conn, ACCESSORY_CATEGORY_ID, "DualShock 4")
    if "заряд" in t and any(x in t for x in ("ps5", "dualsense")):
        return ensure_entry(conn, ACCESSORY_CATEGORY_ID, "Зарядная станция DualSense")
    if "заряд" in t and any(x in t for x in ("ps4", "dualshock")):
        return ensure_entry(conn, ACCESSORY_CATEGORY_ID, "Зарядная станция DualShock 4")
    if "camera" in t or "камера" in t:
        return ensure_entry(conn, ACCESSORY_CATEGORY_ID, "PlayStation Camera")
    if "portal" in t:
        return ensure_entry(conn, ACCESSORY_CATEGORY_ID, "PlayStation Portal")
    if "vr" in t or "psvr" in t:
        return ensure_entry(conn, ACCESSORY_CATEGORY_ID, "PlayStation VR")
    return None


def clean_price_observations(conn) -> None:
    rows = conn.execute(
        """
        select id, item_title, raw_json::jsonb->>'item_type' as item_type
        from price_observations
        where observation_type='avito_market'
        """
    ).fetchall()
    bad_ids: list[int] = []
    updates: list[tuple[int, int]] = []
    for obs_id, title, item_type in rows:
        title = str(title or "")
        t = norm(title)
        item_type = str(item_type or "")
        if item_type == "console":
            target = classify_console_catalog_id(conn, title)
            if target and any(x in t for x in ("playstation", "ps4", "ps5", "пс4", "пс5")):
                updates.append((target, int(obs_id)))
            else:
                bad_ids.append(int(obs_id))
        elif item_type in {"controller", "accessory"}:
            target = classify_accessory_catalog_id(conn, title)
            if target:
                updates.append((target, int(obs_id)))
            else:
                # Keep obscure accessories in archive, but do not use them as auto-price anchors.
                bad_ids.append(int(obs_id))
    for target_id, obs_id in updates:
        conn.execute("update price_observations set catalog_entry_id=%s where id=%s", (target_id, obs_id))
    if bad_ids:
        conn.execute(
            "update price_observations set usable_for_auto_price=false where id = any(%s)",
            (bad_ids,),
        )


def main() -> int:
    with psycopg.connect(DSN) as conn:
        for canonical in CANONICALS:
            entry_id = ensure_entry(conn, canonical.category_id, canonical.name)
            for alias in canonical.aliases:
                ensure_alias(conn, entry_id, alias)
        for category_id, target_name, source_names in MERGES:
            merge_entry(conn, category_id, target_name, source_names)
        clean_price_observations(conn)
        conn.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
