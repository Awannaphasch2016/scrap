"""Suburb-search scraper — opencli substrate.

For each watched (suburb, state), paginate /buy/in-<suburb>+<state>/list-N?sortOrder=Newest
through `opencli browser realestate --window background open + eval`, and build CardSnapshot
objects with fingerprints.

Why opencli (not headless Playwright): realestate.com.au is fronted by Kasada bot protection.
Headless Playwright gets HTTP 429 + KPSDK challenge; opencli drives the user's real Chrome
session and bypasses Kasada cleanly. Verified empirically 2026-06-11 BKK.

Early-exit: stop paginating when ≥80% of cards on a page were already seen on a prior page in
the same run (we've passed the "Newest" frontier).

Prereq: `opencli` on PATH, opencli daemon running, a Chrome session named "realestate" already
open (use `opencli browser realestate --window background open <url>` once to establish it).
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import time
from typing import Iterable

from .. import config
from ..types import CardSnapshot

log = logging.getLogger(__name__)

SESSION = "realestate"
OPENCLI_TIMEOUT_S = 90


# JS executed inside the page to extract every ResidentialCard.
# Defensive selectors with fall-backs — class names drift across renders.
EXTRACT_JS = r"""
(() => {
  const cards = Array.from(document.querySelectorAll('[data-testid="ResidentialCard"]'));
  return JSON.stringify(cards.map((card, idx) => {
    const linkEl = card.querySelector('a[href*="/property-"]') || card.querySelector('h2 a');
    const url = linkEl ? linkEl.href : null;
    const address = linkEl ? (linkEl.textContent || '').trim() : null;

    const priceEl = card.querySelector('.residential-card__price, .property-price, [data-testid="price-wrapper"]');
    const price = priceEl ? (priceEl.textContent || '').trim() : '';

    const agentEl = card.querySelector('.agent__name, [class*="AgentName"]');
    const agent = agentEl ? (agentEl.textContent || '').trim() : '';

    const agencyImg = card.querySelector('[class*="branding"] img[alt], [class*="Branding"] img[alt]');
    const agencyText = card.querySelector('[class*="branding-text"], [class*="BrandingText"]');
    const agency = agencyImg ? agencyImg.alt : (agencyText ? (agencyText.textContent || '').trim() : '');

    const featureNodes = Array.from(card.querySelectorAll(
      '.general-features__feature, [class*="general-features"] span, [class*="property-info__property-attributes"] span, [class*="GeneralFeatures"] span'
    ));
    const features = featureNodes
      .map(el => (el.textContent || '').trim())
      .filter(t => t && t.length < 20);

    const typeEl = card.querySelector('.residential-card__property-type, [class*="property-type"], [class*="PropertyType"]');
    const propertyType = typeEl ? (typeEl.textContent || '').trim() : '';

    const badgeEl = card.querySelector('.residential-card__banner-strip, [class*="banner-strip"], [class*="BannerStrip"]');
    const status_badge = badgeEl ? (badgeEl.textContent || '').trim() : '';

    return { rank: idx + 1, url, address, price, agent, agency, features, property_type: propertyType, status_badge };
  }));
})()
"""


# opencli wraps its eval output in a JSON envelope like {"value": "<string>"} when the
# expression returns a string, or {"value": [...]} when it returns an array. We always
# wrap in JSON.stringify so the envelope's `value` is a JSON string we can re-parse.
def _opencli_eval(js: str) -> object:
    cmd = ["opencli", "browser", SESSION, "--window", "background", "eval", js]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=OPENCLI_TIMEOUT_S
    )
    if proc.returncode != 0:
        log.error("opencli eval failed · rc=%d · stderr=%s", proc.returncode, proc.stderr[:400])
        return None
    raw = proc.stdout.strip()
    # opencli emits primitives like `undefined\n` for void expressions — silently treat as None.
    if raw in ("undefined", "null", ""):
        return None
    try:
        outer = json.loads(raw)
    except Exception:
        m = re.search(r"(\{.*?\})|(\".*?\")", raw, re.DOTALL)
        if not m:
            log.warning("could not locate JSON in opencli stdout · raw=%r", raw[:200])
            return None
        try:
            outer = json.loads(m.group(0))
        except Exception:
            log.warning("opencli stdout JSON parse fail · raw=%r", raw[:200])
            return None
    # outer is either a primitive (number/string) or a string-wrapped JSON we need to re-parse.
    if isinstance(outer, str):
        try:
            return json.loads(outer)
        except Exception:
            return outer
    return outer


def _opencli_open(url: str) -> bool:
    cmd = ["opencli", "browser", SESSION, "--window", "background", "open", url]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=OPENCLI_TIMEOUT_S)
    return proc.returncode == 0


def _strip_prefix(url: str) -> str:
    """Drop /buy/ or /sold/ prefix so the URL is stable across SOLD transitions.

    Active: https://www.realestate.com.au/property-apartment-nsw-sydney-151321152
    Sold:   https://www.realestate.com.au/sold/property-studio-nsw-sydney-151321152
    Both stripped to: https://www.realestate.com.au/property-...-151321152
    The trailing numeric ID stays stable across transitions.
    """
    return re.sub(r"https?://www\.realestate\.com\.au/(buy|sold)/", "https://www.realestate.com.au/", url)


def _parse_features(features: list[str]) -> tuple[int | None, int | None, int | None]:
    nums: list[int | None] = []
    for f in features[:3]:
        m = re.match(r"^\s*(\d+)", f)
        nums.append(int(m.group(1)) if m else None)
    while len(nums) < 3:
        nums.append(None)
    return nums[0], nums[1], nums[2]


def _parse_suburb_state_from_address(address: str) -> tuple[str | None, str | None, str | None]:
    if not address:
        return None, None, None
    parts = [p.strip() for p in address.split(",")]
    if len(parts) < 2:
        return None, None, None
    suburb = parts[-2] if len(parts) >= 3 else None
    last = parts[-1]
    m = re.match(r"^([A-Z]{2,3})\s*(\d{4})?$", last)
    state = m.group(1).lower() if m else None
    postcode = m.group(2) if m and m.group(2) else None
    suburb_slug = re.sub(r"\s+", "-", (suburb or "").lower()) if suburb else None
    return suburb_slug, state, postcode


def _card_to_snapshot(card: dict, suburb_slug: str, state_slug: str) -> CardSnapshot | None:
    url = card.get("url") or ""
    address = card.get("address") or ""
    if not url or not address:
        return None
    canonical = _strip_prefix(url)
    bed, bath, car = _parse_features(card.get("features") or [])
    sub_from_addr, state_from_addr, postcode = _parse_suburb_state_from_address(address)
    return CardSnapshot(
        canonical_url=canonical,
        listing_url=url,
        address=address,
        suburb_slug=sub_from_addr or suburb_slug,
        state_slug=state_from_addr or state_slug,
        price_text=card.get("price") or "",
        agent_name=card.get("agent") or "",
        agency_name=card.get("agency") or "",
        bed=bed,
        bath=bath,
        car=car,
        property_type=card.get("property_type") or None,
        status_badge=card.get("status_badge") or "",
        raw_card={**card, "postcode": postcode},
    )


def _scrape_one_page(suburb: str, state: str, page_num: int) -> list[dict]:
    url = config.SUBURB_SEARCH_URL.format(suburb=suburb, state=state, page=page_num)
    log.info("GET %s", url)
    if not _opencli_open(url):
        log.warning("opencli open failed · %s", url)
        return []
    time.sleep(5)  # allow page render + lazy-load
    # Trigger lazy-load by scrolling, then extract.
    _opencli_eval("(() => { window.scrollTo(0, document.body.scrollHeight); 'ok'; })()")
    time.sleep(1.5)
    _opencli_eval("(() => { window.scrollTo(0, 0); 'ok'; })()")
    time.sleep(0.5)
    result = _opencli_eval(EXTRACT_JS)
    if result is None:
        return []
    if not isinstance(result, list):
        log.warning("unexpected eval result type · %s", type(result).__name__)
        return []
    return result


def scrape_suburb(
    suburb: str,
    state: str,
    max_pages: int = config.MAX_PAGES_DEFAULT,
) -> list[CardSnapshot]:
    out: list[CardSnapshot] = []
    seen_urls: set[str] = set()
    for n in range(1, max_pages + 1):
        cards = _scrape_one_page(suburb, state, n)
        if not cards:
            log.info("empty page %d for %s,%s — stopping", n, suburb, state)
            break
        page_snaps: list[CardSnapshot] = []
        overlap = 0
        for c in cards:
            snap = _card_to_snapshot(c, suburb, state)
            if snap is None:
                continue
            if snap.canonical_url in seen_urls:
                overlap += 1
                continue
            seen_urls.add(snap.canonical_url)
            page_snaps.append(snap)
        out.extend(page_snaps)
        log.info("page %d · %d cards (%d overlap from prior pages)", n, len(page_snaps), overlap)
        if overlap / max(len(cards), 1) >= config.PAGE_OVERLAP_EARLY_EXIT:
            log.info("overlap >= %d%% — early-exit pagination", int(100 * config.PAGE_OVERLAP_EARLY_EXIT))
            break
    return out


def scrape_all_watches(
    watches: Iterable[tuple[str, str]],
    max_pages: int = config.MAX_PAGES_DEFAULT,
) -> list[CardSnapshot]:
    snapshots: list[CardSnapshot] = []
    t0 = time.time()
    for suburb, state in watches:
        log.info("=== scraping %s, %s ===", suburb, state)
        snaps = scrape_suburb(suburb, state, max_pages=max_pages)
        log.info("%s,%s · %d snapshots", suburb, state, len(snaps))
        snapshots.extend(snaps)
    log.info("scrape_all_watches done · %d snapshots · %.1fs", len(snapshots), time.time() - t0)
    return snapshots


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    argv = sys.argv[1:]
    suburb = argv[0] if argv else "sydney-cbd"
    state = argv[1] if len(argv) > 1 else "nsw"
    pages = int(argv[2]) if len(argv) > 2 else 1
    snaps = scrape_all_watches([(suburb, state)], max_pages=pages)
    print(f"\n=== {len(snaps)} snapshots ===")
    for s in snaps[:5]:
        print(f"  {s.address} · {s.price_text} · agent={s.agent_name!r} · fp={s.compute_fingerprint()[:10]}")
