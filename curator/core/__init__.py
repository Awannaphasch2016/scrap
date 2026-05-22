"""Topic-agnostic curator substrate.

Public surface (re-exported here for convenience; each module is also
importable directly):
  - Source, Item                                        — curator.core.types
  - Fetcher, _proxies, _strip_html, _parse_iso, _parse_rfc822
                                                        — curator.core.fetcher
"""

from curator.core.fetcher import Fetcher, _parse_iso, _parse_rfc822, _proxies, _strip_html
from curator.core.store import Store
from curator.core.types import Item, Source

__all__ = [
    "Source", "Item",
    "Fetcher", "_proxies", "_strip_html", "_parse_iso", "_parse_rfc822",
    "Store",
]
