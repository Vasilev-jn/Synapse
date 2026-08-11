from __future__ import annotations

from datetime import datetime

import psycopg
from psycopg.types.json import Json


DSN = "postgresql://postgres:postgres@127.0.0.1:5432/crm_inventory"

BASELINES = {
    "PlayStation 4 Fat 500GB": 11000,
    "PlayStation 4 Slim 500GB": 15000,
    "PlayStation 4 Pro 1TB": 20000,
    "PlayStation 5 Digital": 45000,
    "DualShock 4": 1275,
    "DualSense": 4000,
    "Зарядная станция DualShock 4": 1145,
}


def main() -> int:
    now = datetime.now()
    with psycopg.connect(DSN) as conn:
        for name, price in BASELINES.items():
            row = conn.execute(
                "select id from catalog_entries where lower(name)=lower(%s) order by id limit 1",
                (name,),
            ).fetchone()
            if not row:
                print(f"[SKIP] no catalog entry: {name}")
                continue
            catalog_entry_id = int(row[0])
            source = "kirill_market_baseline"
            source_uid = f"manual-baseline:{catalog_entry_id}:{name}"
            existing = conn.execute(
                "select id from price_observations where source=%s and source_uid=%s",
                (source, source_uid),
            ).fetchone()
            raw_json = {
                "item_type": "console" if name.startswith("PlayStation") else "accessory",
                "canonical_name": name,
                "source_kind": "manual_market_baseline",
                "price_confidence": "high",
                "price_source_type": "manual_sale_price_expectation",
                "price_scope": "per_item",
                "use_for_market_pricing": True,
                "notes": "Kirill factual expected sale median from real selling experience",
            }
            if existing:
                conn.execute(
                    """
                    update price_observations
                    set item_title=%s, price=%s, currency='RUB', observed_at=%s,
                        confidence=0.95, usable_for_auto_price=true,
                        catalog_entry_id=%s, raw_json=%s::json, updated_at=%s
                    where id=%s
                    """,
                    (name, price, now, catalog_entry_id, Json(raw_json), now, int(existing[0])),
                )
            else:
                conn.execute(
                    """
                    insert into price_observations
                    (source, source_uid, observation_type, item_title, price, currency, observed_at,
                     confidence, usable_for_auto_price, catalog_entry_id, raw_json, created_at, updated_at)
                    values (%s,%s,'own_avito_active',%s,%s,'RUB',%s,0.95,true,%s,%s::json,%s,%s)
                    """,
                    (source, source_uid, name, price, now, catalog_entry_id, Json(raw_json), now, now),
                )
            print(f"[OK] {name}: {price}")
        conn.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
