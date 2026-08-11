from __future__ import annotations

import sys
from pathlib import Path

CRM_ROOT = Path("C:/crm_inventory")
if str(CRM_ROOT) not in sys.path:
    sys.path.insert(0, str(CRM_ROOT))

from app.config import get_settings  # type: ignore  # noqa: E402
from app.db import get_engine, make_session_factory  # type: ignore  # noqa: E402
from app.models import PriceObservation  # type: ignore  # noqa: E402


METADATA_KEYS = (
    "reservation_status",
    "posted_at",
    "posted_at_text",
    "is_reserved",
    "is_active",
    "finish_time",
    "avito_server_date",
    "monitor_context",
)


def main() -> int:
    settings = get_settings()
    engine = get_engine(settings=settings)
    SessionLocal = make_session_factory(engine)
    scanned = 0
    updated = 0
    with SessionLocal() as session:
        observations = session.query(PriceObservation).filter(PriceObservation.market_listing_id.is_not(None)).all()
        for observation in observations:
            scanned += 1
            listing = observation.market_listing
            if listing is None:
                continue
            listing_raw = listing.raw_json if isinstance(listing.raw_json, dict) else {}
            raw = dict(observation.raw_json) if isinstance(observation.raw_json, dict) else {}
            changed = False
            if listing.url and raw.get("listing_url") != listing.url:
                raw["listing_url"] = listing.url
                changed = True
            if listing.external_id and raw.get("listing_external_id") != listing.external_id:
                raw["listing_external_id"] = listing.external_id
                changed = True
            for key in METADATA_KEYS:
                if raw.get(key) is None and listing_raw.get(key) is not None:
                    raw[key] = listing_raw.get(key)
                    changed = True
            if changed:
                observation.raw_json = raw
                updated += 1
        session.commit()
    print(f"[BACKFILL] scanned={scanned} updated={updated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
