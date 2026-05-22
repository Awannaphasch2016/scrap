"""RemoteOK fetcher · https://remoteok.com/api returns one big JSON array."""

from __future__ import annotations

import logging
from typing import Iterable

from curator.core.fetcher import (
    DEFAULT_USER_AGENT, Fetcher, _get_json, _parse_iso, _strip_html,
)
from curator.core.types import Item, Source

logger = logging.getLogger(__name__)


class RemoteOKFetcher(Fetcher):
    URL = "https://remoteok.com/api"

    def __init__(self, *, user_agent: str = DEFAULT_USER_AGENT) -> None:
        self.user_agent = user_agent

    def fetch(self, source: Source, source_cfg: dict, cutoff_ts: float) -> Iterable[Item]:
        data = _get_json(self.URL, user_agent=self.user_agent)
        if not isinstance(data, list):
            logger.warning("remoteok: unexpected payload shape")
            return
        # First element is metadata; skip if it lacks "id"
        for j in data:
            if not isinstance(j, dict) or "id" not in j:
                continue
            iso = j.get("date") or ""
            ts = _parse_iso(iso)
            if ts < cutoff_ts:
                continue
            tags = j.get("tags") or []
            descr = _strip_html(j.get("description") or "")
            company = j.get("company") or ""
            position = j.get("position") or j.get("title") or ""
            url = j.get("url") or j.get("apply_url") or ""
            yield Item(
                id=f"remoteok:{j['id']}",
                source_id=source.id,
                type="job",
                title=f"{position} @ {company}".strip(" @"),
                url=url,
                author=company,
                content=descr,
                published_at=ts,
                metadata={
                    "tags": tags,
                    "salary_min": j.get("salary_min"),
                    "salary_max": j.get("salary_max"),
                    "location": j.get("location"),
                },
            )
