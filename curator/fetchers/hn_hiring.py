"""Hacker News "Ask HN: Who is hiring?" monthly thread fetcher.

Finds the current month's canonical thread via Algolia's date-sorted search,
then streams comments newer than the cutoff. Skips obvious seeker-side
comments ("seeking work" / "looking for work") because they're the opposite
of what a jobs topic wants.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Iterable

from curator.core.fetcher import DEFAULT_USER_AGENT, Fetcher, _get_json, _strip_html
from curator.core.types import Item, Source

logger = logging.getLogger(__name__)


class HNHiringFetcher(Fetcher):
    SEARCH_URL = (
        "https://hn.algolia.com/api/v1/search_by_date"
        "?query=Ask+HN+Who+is+hiring&tags=story&hitsPerPage=30"
    )
    # Canonical monthly thread by whoishiring bot: 'Ask HN: Who is hiring? (Month YYYY)'.
    THREAD_TITLE_RE = re.compile(
        r"^ask hn:\s*who is hiring\?\s*\([a-z]+\s+\d{4}\)\s*$", re.IGNORECASE
    )
    COMMENTS_URL_TMPL = (
        "https://hn.algolia.com/api/v1/search"
        "?tags=comment,story_{story_id}&hitsPerPage=1000&numericFilters=created_at_i>={cutoff}"
    )

    def __init__(self, *, user_agent: str = DEFAULT_USER_AGENT) -> None:
        self.user_agent = user_agent

    def fetch(self, source: Source, source_cfg: dict, cutoff_ts: float) -> Iterable[Item]:
        data = _get_json(self.SEARCH_URL, user_agent=self.user_agent)
        hits = data.get("hits", []) if isinstance(data, dict) else []
        story = next(
            (h for h in hits if self.THREAD_TITLE_RE.match(h.get("title", "").strip())),
            None,
        )
        if not story:
            logger.warning("hn_hiring: no current monthly thread found in %d hits", len(hits))
            return
        story_id = story["objectID"]
        story_title = story.get("title", "Who is hiring")
        story_ts = story.get("created_at_i", 0)
        logger.info(
            "hn_hiring: thread %s (%s) — %s",
            story_id, story_title,
            datetime.fromtimestamp(story_ts, timezone.utc).strftime("%Y-%m"),
        )

        url = self.COMMENTS_URL_TMPL.format(story_id=story_id, cutoff=int(cutoff_ts))
        comments = _get_json(url, user_agent=self.user_agent)
        for c in comments.get("hits", []) if isinstance(comments, dict) else []:
            body_html = c.get("comment_text") or ""
            body = _strip_html(body_html)
            if not body:
                continue
            head = body[:200].lower()
            if "seeking work" in head or "looking for work" in head or "i'm seeking" in head:
                continue
            obj_id = c.get("objectID")
            permalink = f"https://news.ycombinator.com/item?id={obj_id}"
            title = body.split("\n", 1)[0][:140].strip()
            yield Item(
                id=f"hn:{obj_id}",
                source_id=source.id,
                type="job",
                title=title or "Untitled HN listing",
                url=permalink,
                author=c.get("author", ""),
                content=body,
                published_at=float(c.get("created_at_i", 0)),
                metadata={"thread_id": story_id, "thread_title": story_title},
            )
