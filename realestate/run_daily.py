"""Daily orchestrator — scrape → diff → withdrawal pass → (optional) Sheets sync.

CLI:
  python -m realestate.run_daily --dry-store --dry-sheets --suburbs sydney-cbd --max-pages 1
  doppler run --project scrape --config dev -- python -m realestate.run_daily

Flags:
  --dry-store   use in-memory MemoryStore; no Supabase calls (useful for first smoke)
  --dry-sheets  skip Google Sheets sync
  --suburbs     comma-separated suburb-slug list (default: config.WATCHED_SUBURBS_DEFAULT)
  --max-pages   pagination cap per suburb (default: config.MAX_PAGES_DEFAULT)

Exit summary line (parsed by Phase 3 burn-in scorecard):
  [run_daily] done · n_new=X · n_price=Y · n_sold=Z · n_withdrawn=W · t=Ns
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import Counter
from datetime import date

from . import config
from .diff import engine as diff
from .fetchers import listings
from .storage.memory import MemoryStore

log = logging.getLogger("realestate.run_daily")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="realestate.com.au daily tracker — local pipeline")
    p.add_argument("--dry-store", action="store_true", help="use in-memory store; no DB")
    p.add_argument("--dry-sheets", action="store_true", help="skip Google Sheets sync")
    p.add_argument(
        "--suburbs",
        type=str,
        default=None,
        help="comma-separated suburb-slug:state pairs, e.g. 'sydney-cbd:nsw,bondi:nsw'",
    )
    p.add_argument("--max-pages", type=int, default=config.MAX_PAGES_DEFAULT)
    p.add_argument("--log-level", type=str, default="INFO")
    return p.parse_args()


def _parse_suburbs(s: str | None) -> list[tuple[str, str]]:
    if not s:
        return list(config.WATCHED_SUBURBS_DEFAULT)
    out: list[tuple[str, str]] = []
    for item in s.split(","):
        item = item.strip()
        if ":" in item:
            suburb, state = item.split(":", 1)
        else:
            suburb, state = item, "nsw"
        out.append((suburb.strip().lower(), state.strip().lower()))
    return out


def _build_store(dry_store: bool):
    if dry_store:
        return MemoryStore()
    # Real Postgres store wiring — added when Phase 2 (Supabase DSN) is set up.
    raise NotImplementedError(
        "non-dry-store mode requires the Postgres store · run with --dry-store for now"
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args()
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    suburbs = _parse_suburbs(args.suburbs)
    log.info("config · suburbs=%s · max_pages=%d · dry_store=%s · dry_sheets=%s",
             suburbs, args.max_pages, args.dry_store, args.dry_sheets)

    store = _build_store(args.dry_store)
    t0 = time.time()

    # 1. Scrape every watched suburb.
    snapshots = listings.scrape_all_watches(suburbs, max_pages=args.max_pages)

    # 2. Diff against stored fingerprints — emit NEW / PRICE_CHANGED.
    events_change = diff.apply_snapshot(snapshots, store)

    # 3. Withdrawal pass — for properties not seen today AND over the grace period, probe.
    events_withdraw = diff.withdrawal_pass(store, today=date.today())

    # 4. (Phase 2) Sheets sync — deferred.
    if not args.dry_sheets:
        try:
            from .sheets import sync as sheets_sync  # noqa: F401
            log.warning("sheets sync not implemented yet — passing --dry-sheets effectively")
        except ImportError:
            log.warning("sheets module not present — skipping sync")

    # 5. Summary line for the burn-in scorecard.
    all_events = events_change + events_withdraw
    by_type: Counter[str] = Counter(e.event_type for e in all_events)
    elapsed = time.time() - t0
    summary = (
        f"[run_daily] done · n_new={by_type.get('NEW', 0)} "
        f"· n_price={by_type.get('PRICE_CHANGED', 0)} "
        f"· n_sold={by_type.get('SOLD', 0)} "
        f"· n_withdrawn={by_type.get('WITHDRAWN', 0)} "
        f"· t={elapsed:.1f}s"
    )
    print(summary, file=sys.stderr)

    # If --dry-store, also print a few first-page samples for sanity check.
    if args.dry_store and isinstance(store, MemoryStore):
        n_props = len(store.properties)
        log.info("dry-store: %d properties, %d events", n_props, len(store.events))
        for i, ev in enumerate(store.events[:5]):
            log.info("  ev[%d] %s · %s", i, ev.event_type, ev.payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
