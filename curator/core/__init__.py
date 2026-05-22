"""Topic-agnostic curator substrate.

Public surface (re-exported here for convenience; each module is also
importable directly):
  - Source, Item                                        — curator.core.types
  - Fetcher, _proxies, _strip_html, _parse_iso, _parse_rfc822
                                                        — curator.core.fetcher
"""

from curator.core.fetcher import (
    DEFAULT_USER_AGENT,
    Fetcher,
    _get_json,
    _get_text,
    _parse_iso,
    _parse_rfc822,
    _proxies,
    _strip_html,
)
from curator.core.notebooklm import DopplerStorage, NotebookLMUploader
from curator.core.render import _humanize_age
from curator.core.s3 import publish_to_s3
from curator.core.store import Store
from curator.core.types import Item, Source

__all__ = [
    "Source", "Item",
    "Fetcher", "DEFAULT_USER_AGENT",
    "_proxies", "_strip_html", "_parse_iso", "_parse_rfc822", "_get_json", "_get_text",
    "Store",
    "NotebookLMUploader", "DopplerStorage",
    "publish_to_s3",
    "_humanize_age",
]
