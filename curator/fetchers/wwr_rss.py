"""WeWorkRemotely RSS fetcher · category feed URLs supplied per-source via cfg.feed.

WWR exposes one RSS endpoint per category (e.g. .../remote-programming-jobs.rss).
The topic's SOURCES entry carries the feed URL in `cfg.feed`; this fetcher
parses the RSS 2.0 channel and yields one Item per `<item>`.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Iterable

from curator.core.fetcher import (
    DEFAULT_USER_AGENT, Fetcher, _get_text, _parse_rfc822, _strip_html,
)
from curator.core.types import Item, Source


class WWRRSSFetcher(Fetcher):
    def __init__(self, *, user_agent: str = DEFAULT_USER_AGENT) -> None:
        self.user_agent = user_agent

    def fetch(self, source: Source, source_cfg: dict, cutoff_ts: float) -> Iterable[Item]:
        feed_url = source_cfg.get("feed") or source_cfg.get("url")
        if not feed_url:
            return
        text = _get_text(feed_url, user_agent=self.user_agent)
        root = ET.fromstring(text)
        # RSS 2.0 path: rss/channel/item
        channel = root.find("channel")
        if channel is None:
            return
        for item in channel.findall("item"):
            link = (item.findtext("link") or "").strip()
            title = (item.findtext("title") or "").strip()
            descr = _strip_html(item.findtext("description") or "")
            pub = item.findtext("pubDate") or ""
            ts = _parse_rfc822(pub)
            if ts < cutoff_ts:
                continue
            guid = (item.findtext("guid") or link or title).strip()
            yield Item(
                id=f"wwr:{hash(guid) & 0xFFFFFFFF:x}",
                source_id=source.id,
                type="job",
                title=title,
                url=link,
                author="",
                content=descr,
                published_at=ts,
                metadata={"guid": guid},
            )
