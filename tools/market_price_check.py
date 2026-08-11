from __future__ import annotations

import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

import psycopg


DSN = "postgresql://postgres:postgres@127.0.0.1:5432/crm_inventory"


@dataclass
class ListedGame:
    name: str
    your_price: int
    sold: bool = False


GAMES = [
    ListedGame("GTA V", 1200),
    ListedGame("GTA V Premium Edition", 1200),
    ListedGame("FIFA 20", 600),
    ListedGame("NBA 2K16", 900),
    ListedGame("Hogwarts Legacy", 1200),
    ListedGame("God of War", 1000, True),
    ListedGame("Detroit Become Human", 900, True),
    ListedGame("The Last of Us Remastered", 1100),
    ListedGame("Uncharted 4", 950),
    ListedGame("Diablo III Ultimate Evil Edition", 1900),
    ListedGame("Watch Dogs 2", 1200),
    ListedGame("Unknown 9 Awakening", 1700),
    ListedGame("Resident Evil 6", 5500),
    ListedGame("LEGO Jurassic World", 900),
    ListedGame("Horizon Zero Dawn Complete Edition", 900),
    ListedGame("Horizon Zero Dawn", 700),
    ListedGame("Metro Exodus PS5", 1400),
    ListedGame("LEGO City Undercover", 1200),
    ListedGame("Call of Duty Black Ops III", 1900),
    ListedGame("Call of Duty Vanguard", 2300),
    ListedGame("Dying Light 2 Stay Human", 1900),
    ListedGame("Bloodborne", 1000),
    ListedGame("The Division Gold Edition", 1300),
    ListedGame("Just Dance 2018", 1600),
    ListedGame("Just Dance 2017", 1400),
    ListedGame("Rainbow Six Siege", 700),
    ListedGame("Resident Evil Village PS5", 1700),
    ListedGame("Assassin's Creed Shadows Special Edition PS5", 2800),
    ListedGame("Robinson The Journey VR", 1100),
]


ALIASES = {
    "GTA V": ["gta v", "gta 5", "grand theft auto v", "grand theft auto 5"],
    "GTA V Premium Edition": ["gta v premium", "gta 5 premium", "grand theft auto v premium"],
    "FIFA 20": ["fifa 20", "фифа 20"],
    "NBA 2K16": ["nba 2k16", "нба 2к16"],
    "Hogwarts Legacy": ["hogwarts legacy", "хогвартс"],
    "God of War": ["god of war", "бог войны"],
    "Detroit Become Human": ["detroit become human", "детройт"],
    "The Last of Us Remastered": ["last of us remastered", "the last of us remastered", "одни из нас remastered"],
    "Uncharted 4": ["uncharted 4", "анчатед 4"],
    "Diablo III Ultimate Evil Edition": ["diablo iii", "diablo 3", "ultimate evil"],
    "Watch Dogs 2": ["watch dogs 2", "watchdogs 2"],
    "Unknown 9 Awakening": ["unknown 9", "awakening"],
    "Resident Evil 6": ["resident evil 6", "re 6"],
    "LEGO Jurassic World": ["lego jurassic world", "лего мир юрского"],
    "Horizon Zero Dawn Complete Edition": ["horizon zero dawn complete"],
    "Horizon Zero Dawn": ["horizon zero dawn"],
    "Metro Exodus PS5": ["metro exodus"],
    "LEGO City Undercover": ["lego city undercover"],
    "Call of Duty Black Ops III": ["black ops iii", "black ops 3", "call of duty black ops iii"],
    "Call of Duty Vanguard": ["vanguard", "call of duty vanguard"],
    "Dying Light 2 Stay Human": ["dying light 2"],
    "Bloodborne": ["bloodborne"],
    "The Division Gold Edition": ["division gold", "the division gold"],
    "Just Dance 2018": ["just dance 2018"],
    "Just Dance 2017": ["just dance 2017"],
    "Rainbow Six Siege": ["rainbow six siege"],
    "Resident Evil Village PS5": ["resident evil village", "re village"],
    "Assassin's Creed Shadows Special Edition PS5": ["assassin's creed shadows", "assassins creed shadows"],
    "Robinson The Journey VR": ["robinson the journey"],
}


def norm(s: str) -> str:
    return " ".join(s.casefold().replace("ё", "е").replace("’", "'").split())


def load_rows(conn) -> list[dict]:
    rows = conn.execute(
        """
        select
            po.id,
            po.item_title,
            po.platform_or_model,
            po.price,
            po.price_with_delivery,
            po.observation_type,
            po.source,
            po.usable_for_auto_price,
            po.raw_json::jsonb as raw_json,
            ml.title as listing_title,
            ml.url as listing_url
        from price_observations po
        left join market_listings ml on ml.id = po.market_listing_id
        where po.price > 0
        """
    ).fetchall()
    result = []
    for row in rows:
        result.append(dict(zip([col.name for col in conn.execute("select 1").description] if False else [
            "id", "item_title", "platform_or_model", "price", "price_with_delivery",
            "observation_type", "source", "usable_for_auto_price", "raw_json", "listing_title", "listing_url"
        ], row)))
    return result


def load_owned_costs(conn) -> dict[str, list[dict]]:
    rows = conn.execute(
        """
        select
            i.id,
            ce.name,
            i.status,
            i.calculated_cost,
            i.sale_price
        from items i
        left join catalog_entries ce on ce.id = i.catalog_entry_id
        where ce.name is not null
        """
    ).fetchall()
    owned: dict[str, list[dict]] = {}
    for item_id, name, status, cost, sale_price in rows:
        text = norm(str(name or ""))
        row = {
            "id": item_id,
            "name": name,
            "status": status,
            "cost": int(cost) if cost is not None else None,
            "sale_price": int(sale_price) if sale_price is not None else None,
        }
        for game in GAMES:
            terms = [norm(t) for t in ALIASES.get(game.name, [game.name])]
            if any(term and (term in text or text in term) for term in terms):
                owned.setdefault(game.name, []).append(row)
    return owned


def text_for_row(row: dict) -> str:
    parts = [
        row.get("item_title"),
        row.get("platform_or_model"),
        row.get("listing_title"),
    ]
    raw = row.get("raw_json")
    if isinstance(raw, dict):
        parts += [
            raw.get("name"),
            raw.get("canonical_name"),
            raw.get("listing_title"),
            raw.get("price_source_text"),
        ]
    return norm(" ".join(str(p or "") for p in parts))


def prices_for(game: ListedGame, rows: list[dict], *, usable_only: bool) -> list[float]:
    terms = [norm(t) for t in ALIASES.get(game.name, [game.name])]
    found = []
    for row in rows:
        if usable_only and not row.get("usable_for_auto_price"):
            continue
        text = text_for_row(row)
        if any(term and term in text for term in terms):
            found.append(float(row["price"]))
    return found


def summarize(prices: list[float]) -> dict[str, float | int | None]:
    if not prices:
        return {"n": 0, "min": None, "median": None, "avg": None, "max": None}
    ordered = sorted(prices)
    return {
        "n": len(ordered),
        "min": round(ordered[0]),
        "median": round(statistics.median(ordered)),
        "avg": round(statistics.mean(ordered)),
        "max": round(ordered[-1]),
    }


def main() -> int:
    with psycopg.connect(DSN) as conn:
        total, usable = conn.execute(
            "select count(*), count(*) filter (where usable_for_auto_price) from price_observations"
        ).fetchone()
        rows = load_rows(conn)
        owned_costs = load_owned_costs(conn)

    print(f"total_observations={total} usable_for_auto_price={usable}")
    print(
        "game\tyour\tsold\tcosts_available\tusable_n\tusable_median\t"
        "net_after_sale_87pct\tprofit_min_cost\tprofit_max_cost\tall_n\tall_median\tdiff_vs_usable_median"
    )
    for game in GAMES:
        usable_prices = prices_for(game, rows, usable_only=True)
        all_prices = prices_for(game, rows, usable_only=False)
        us = summarize(usable_prices)
        al = summarize(all_prices)
        diff = None if us["median"] is None else game.your_price - int(us["median"])
        available_costs = [
            row["cost"]
            for row in owned_costs.get(game.name, [])
            if row.get("cost") is not None and int(row["cost"]) > 0 and "ÐŸÑ€Ð¾Ð´Ð°Ð½" not in str(row.get("status"))
        ]
        cost_text = ",".join(str(cost) for cost in sorted(set(available_costs))) if available_costs else ""
        net_after_sale = None if us["median"] is None else round(int(us["median"]) * 0.87)
        profit_min = None
        profit_max = None
        if net_after_sale is not None and available_costs:
            profit_min = net_after_sale - max(available_costs)
            profit_max = net_after_sale - min(available_costs)
        print(
            f"{game.name}\t{game.your_price}\t{game.sold}\t{cost_text}\t{us['n']}\t{us['median']}\t"
            f"{net_after_sale}\t{profit_min}\t{profit_max}\t{al['n']}\t{al['median']}\t{diff}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
