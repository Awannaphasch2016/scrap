"""Constants for the realestate tracker.

Edit `WATCHED_SUBURBS_DEFAULT` to change the burn-in seed set.
Edit `FINGERPRINT_FIELDS` only if Phase 3 criterion C3 (fingerprint stability) fails.
"""

from __future__ import annotations

# Seed suburbs for local dev / first burn-in day if re_watch is empty.
WATCHED_SUBURBS_DEFAULT: list[tuple[str, str]] = [
    ("sydney-cbd", "nsw"),
    ("bondi", "nsw"),
    ("millers-point", "nsw"),
]

# The four canonical fields that go into the fingerprint hash.
# Volatile fields (days_listed, inspection times, photo URLs, "Just listed" badges)
# are deliberately EXCLUDED — including them would flip fingerprints every day.
FINGERPRINT_FIELDS: tuple[str, ...] = (
    "canonical_url",
    "price_text",
    "agent_name",
    "status_badge",
)

# 2 missed scrape days before a SUSPECT_WITHDRAWN row is probed against the canonical page.
WITHDRAWN_GRACE_DAYS: int = 2

# Pagination cap per suburb per run. Override via `--max-pages` CLI flag.
MAX_PAGES_DEFAULT: int = 4

# Stop paginating when this fraction of cards on a page were seen yesterday
# (we've passed the "Newest" frontier).
PAGE_OVERLAP_EARLY_EXIT: float = 0.8

# Google Sheets — Phase 2 wiring.
SHEET_TAB_PROPERTIES: str = "Properties"
SHEET_TAB_EVENTS: str = "Events"
SHEET_BATCH_SIZE: int = 500

# realestate.com.au URL patterns.
SUBURB_SEARCH_URL = (
    "https://www.realestate.com.au/buy/in-{suburb}+{state}/list-{page}?sortOrder=Newest"
)
CANONICAL_PROPERTY_URL = "https://www.realestate.com.au/property/{slug}/"

# Playwright tuning.
PAGE_NAV_TIMEOUT_MS: int = 25_000
PAGE_SELECTOR_TIMEOUT_MS: int = 15_000
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
