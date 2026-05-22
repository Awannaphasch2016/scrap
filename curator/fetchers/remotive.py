"""Remotive fetcher · https://remotive.com/api/remote-jobs returns {"jobs": [...]}."""

from __future__ import annotations

from typing import Iterable

from curator.core.fetcher import (
    DEFAULT_USER_AGENT, Fetcher, _get_json, _parse_iso, _strip_html,
)
from curator.core.types import Item, Source


class RemotiveFetcher(Fetcher):
    URL = "https://remotive.com/api/remote-jobs"

    def __init__(self, *, user_agent: str = DEFAULT_USER_AGENT) -> None:
        self.user_agent = user_agent

    def fetch(self, source: Source, source_cfg: dict, cutoff_ts: float) -> Iterable[Item]:
        data = _get_json(self.URL, user_agent=self.user_agent)
        jobs = data.get("jobs", []) if isinstance(data, dict) else []
        for j in jobs:
            ts = _parse_iso(j.get("publication_date") or "")
            if ts < cutoff_ts:
                continue
            descr = _strip_html(j.get("description") or "")
            company = j.get("company_name") or ""
            title = j.get("title") or ""
            yield Item(
                id=f"remotive:{j.get('id')}",
                source_id=source.id,
                type="job",
                title=f"{title} @ {company}".strip(" @"),
                url=j.get("url") or "",
                author=company,
                content=descr,
                published_at=ts,
                metadata={
                    "tags": j.get("tags") or [],
                    "category": j.get("category"),
                    "job_type": j.get("job_type"),
                    "candidate_required_location": j.get("candidate_required_location"),
                    "salary": j.get("salary"),
                },
            )
