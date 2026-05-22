"""Shared Fetcher ABC and HTTP/parsing helpers for topic curators.

The `Fetcher` ABC is the contract every source-specific fetcher implements:
yield `Item`s for posts within the cutoff window. Topic curators wire concrete
fetchers (Reddit, HN Algolia, RemoteOK, …) into a dict keyed by `Source.type`.

`_proxies` and `_strip_html` / `_parse_iso` / `_parse_rfc822` are the helpers
every fetcher tends to reach for. They live here so individual fetcher modules
don't grow duplicate copies as new sources land.

Not in this module (deliberate):
  - `_get_json` / `_get_text` — need the topic's USER_AGENT threaded through;
    will move once `TopicConfig` exists to carry that.
  - `USER_AGENT`, `REQUEST_DELAY` — per-topic constants for now.
"""

from __future__ import annotations

import html
import os
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable

import requests

from curator.core.types import Item, Source

DEFAULT_USER_AGENT = "scrap-curator/0.1"


class Fetcher(ABC):
    """Yield `Item`s for one `Source`. Implementations are stateless · the
    instance is reused across runs."""

    @abstractmethod
    def fetch(self, source: Source, source_cfg: dict, cutoff_ts: float) -> Iterable[Item]:
        ...


def _get_json(
    url: str, *, user_agent: str = DEFAULT_USER_AGENT, timeout: int = 20
) -> dict | list:
    """GET `url` and return parsed JSON. Raises on non-2xx."""
    r = requests.get(
        url,
        headers={"User-Agent": user_agent, "Accept": "application/json"},
        timeout=timeout,
        proxies=_proxies(),
    )
    r.raise_for_status()
    return r.json()


def _get_text(
    url: str, *, user_agent: str = DEFAULT_USER_AGENT, timeout: int = 20
) -> str:
    """GET `url` and return body as text. Raises on non-2xx."""
    r = requests.get(
        url,
        headers={"User-Agent": user_agent},
        timeout=timeout,
        proxies=_proxies(),
    )
    r.raise_for_status()
    return r.text


def _proxies() -> dict | None:
    """Build a requests-style proxies dict from HTTP_PROXY / HTTPS_PROXY env.

    Returns None when no proxy is configured (direct egress). Used by both
    news and jobs fetchers so the Lambda's Tailscale → laptop tinyproxy chain
    is honored uniformly.
    """
    http_proxy = os.environ.get("HTTP_PROXY")
    if not http_proxy:
        return None
    return {"http": http_proxy, "https": os.environ.get("HTTPS_PROXY", http_proxy)}


def _strip_html(s: str) -> str:
    """Strip HTML to plain text, preserving paragraph breaks via newlines.

    Light-touch · not a full DOM parser. Good enough for job board descriptions
    and RSS item bodies where the HTML is mostly `<p>` and `<br>`.
    """
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</p\s*>", "\n\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()


def _parse_iso(s: str) -> float:
    """Parse an ISO 8601 timestamp to a UTC unix epoch float. 0.0 on failure.

    Handles both `Z` suffix (RemoteOK) and naive-without-tz (Remotive).
    """
    if not s:
        return 0.0
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return 0.0


def _parse_rfc822(s: str) -> float:
    """Parse an RFC 822 timestamp (RSS `pubDate` shape) to a UTC unix epoch."""
    if not s:
        return 0.0
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError):
        return 0.0
