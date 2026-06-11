"""Canonical address-page probe — discriminates SOLD vs STILL_LISTED vs WITHDRAWN.

Called by `diff.engine.withdrawal_pass` for properties that have missed `WITHDRAWN_GRACE_DAYS`
consecutive scrapes. Drives the same `opencli browser realestate` session as the listings
scraper (Kasada bypass — same reason).

Strategy (v1, simple — refined if Phase 3 C5 fails):
  1. Open the LISTING URL we already have stored (e.g. /property-apartment-nsw-sydney-X).
  2. realestate.com.au will either:
       a. Serve the active listing → STILL_LISTED (probably dropped off "Newest" sort)
       b. 302 to /sold/property-... and serve banner "Sold on DD MMM YYYY" → SOLD
       c. 404 / "no longer available" page → WITHDRAWN (or expired)
  3. Parse the visible state via opencli eval; return ProbeResult.

We do NOT separately resolve the /property/<addr-slug>/ aggregate page in v1 — the listing
URL itself contains the SOLD signal because the site auto-redirects /property-X to
/sold/property-X when the listing sells. This avoids slug derivation entirely.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from datetime import date, datetime
from typing import Optional

from ..types import ProbeResult

log = logging.getLogger(__name__)

SESSION = "realestate"
OPENCLI_TIMEOUT_S = 60


PROBE_JS = r"""
(() => {
  const finalUrl = location.href;
  const bodyText = (document.body.innerText || '').slice(0, 4000);
  // SOLD signals
  const isSoldUrl = /\/sold\//.test(finalUrl);
  const bannerEl = document.querySelector('.residential-card__banner-strip, [class*="banner-strip"]');
  const banner = bannerEl ? (bannerEl.textContent || '').trim() : '';
  // Look for "Sold on DD Month YYYY" or "Sold DD Month YYYY"
  const soldOnMatch = bodyText.match(/Sold\s+(?:on\s+)?(\d{1,2}\s+\w+\s+\d{4})/i);
  // Look for a leading "$XXX,XXX" near "Sold"
  const priceMatch = bodyText.match(/(\$[\d,]+(?:\.\d+)?[KkMm]?)[^\n]*\bSold\s+(?:on\s+)?\d/i)
                  || bodyText.match(/\bSold\s+(?:on\s+)?\d[^\n]*\n\s*(\$[\d,]+(?:\.\d+)?[KkMm]?)/i);
  // Withdrawn / not-available signals
  const notAvailable = /no longer (?:available|on the market)|listing has been removed|withdrawn from the market/i.test(bodyText);
  const is404 = /\b404\b|page not found|we can't find that page/i.test(document.title || '');
  return JSON.stringify({
    finalUrl,
    isSoldUrl,
    banner,
    soldOnText: soldOnMatch ? soldOnMatch[1] : null,
    soldPrice: priceMatch ? priceMatch[1] : null,
    notAvailable,
    is404,
    bodyLen: (document.body.innerText || '').length,
  });
})()
"""


def _opencli_open(url: str) -> bool:
    cmd = ["opencli", "browser", SESSION, "--window", "background", "open", url]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=OPENCLI_TIMEOUT_S)
    return proc.returncode == 0


def _opencli_eval(js: str) -> object:
    cmd = ["opencli", "browser", SESSION, "--window", "background", "eval", js]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=OPENCLI_TIMEOUT_S)
    if proc.returncode != 0:
        log.error("opencli eval failed rc=%d", proc.returncode)
        return None
    raw = proc.stdout.strip()
    if raw in ("undefined", "null", ""):
        return None
    try:
        outer = json.loads(raw)
    except Exception:
        m = re.search(r"(\{.*?\})|(\".*?\")", raw, re.DOTALL)
        if not m:
            return None
        try:
            outer = json.loads(m.group(0))
        except Exception:
            return None
    if isinstance(outer, str):
        try:
            return json.loads(outer)
        except Exception:
            return outer
    return outer


def _parse_sold_date(text: str) -> Optional[date]:
    """'10 June 2026' or '10 Jun 2026' -> date."""
    if not text:
        return None
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    return None


def probe_property(listing_url: str) -> ProbeResult:
    """Open the listing's URL and read whether it now shows SOLD / STILL_LISTED / NOT_FOUND."""
    log.info("probe %s", listing_url)
    if not _opencli_open(listing_url):
        log.warning("opencli open failed · %s", listing_url)
        return ProbeResult(outcome="NOT_FOUND")
    time.sleep(4)
    # Trigger lazy-load
    _opencli_eval("(() => { window.scrollTo(0, document.body.scrollHeight / 2); 'ok'; })()")
    time.sleep(1)
    result = _opencli_eval(PROBE_JS)
    if not isinstance(result, dict):
        log.warning("probe got non-dict result · %r", result)
        return ProbeResult(outcome="NOT_FOUND")

    if result.get("is404") or (result.get("bodyLen", 0) < 200):
        return ProbeResult(outcome="NOT_FOUND")
    if result.get("notAvailable"):
        # Listing actively says "no longer available" — we treat this as the listing being
        # removed without sale info. withdrawal_pass will classify as WITHDRAWN.
        return ProbeResult(outcome="NOT_FOUND")

    sold_signal = bool(
        result.get("isSoldUrl")
        or (result.get("banner") and "sold" in result["banner"].lower())
        or result.get("soldOnText")
    )
    if sold_signal:
        sold_date = _parse_sold_date(result.get("soldOnText") or "")
        sold_price = result.get("soldPrice")
        return ProbeResult(outcome="SOLD", sold_price=sold_price, sold_date=sold_date)

    return ProbeResult(outcome="STILL_LISTED")


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    url = sys.argv[1] if len(sys.argv) > 1 else (
        "https://www.realestate.com.au/sold/property-studio-nsw-sydney-151321152"
    )
    r = probe_property(url)
    print(f"outcome={r.outcome}  sold_price={r.sold_price}  sold_date={r.sold_date}")
