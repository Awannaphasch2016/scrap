"""Reddit subreddit fetcher · jobs-shape · filters by flair, drops seekers.

Different shape from reddit_posts.RedditFetcher · this one:
  - Filters by `require_flair` in the source cfg (e.g. "Hiring" for r/forhire).
  - Drops "[For Hire]" / "[FH]" / "Seeking work" titles and flairs — those
    are freelancers offering services, not jobs to apply to.
  - Skips comments entirely · job ads carry the relevant info in the post body.
"""

from __future__ import annotations

import html
import logging
import re
import time
from typing import Iterable

import requests

from curator.core.fetcher import DEFAULT_USER_AGENT, Fetcher, _get_json
from curator.core.types import Item, Source

logger = logging.getLogger(__name__)


class RedditJobsFetcher(Fetcher):
    _SEEKER_TITLE_RE = re.compile(
        r"^\s*\[(?:for[\s-]*hire|fh|seeking|hire\s*me|hireme)\b", re.IGNORECASE
    )
    _SEEKER_FLAIR_RE = re.compile(
        r"\b(for[\s-]*hire|seeking\s*work|hire\s*me)\b", re.IGNORECASE
    )

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        request_delay: float = 1.0,
    ) -> None:
        self.user_agent = user_agent
        self.request_delay = request_delay

    def fetch(self, source: Source, source_cfg: dict, cutoff_ts: float) -> Iterable[Item]:
        subreddit = source_cfg["subreddit"]
        require_flair = source_cfg.get("require_flair")
        after = None
        while True:
            url = f"https://www.reddit.com/r/{subreddit}/new.json?limit=100"
            if after:
                url += f"&after={after}"
            try:
                data = _get_json(url, user_agent=self.user_agent, timeout=20)
            except requests.RequestException as e:
                logger.warning("reddit:%s fetch failed: %s", subreddit, e)
                return
            if not isinstance(data, dict):
                return
            children = data.get("data", {}).get("children", [])
            if not children:
                return
            page_ended = False
            for c in children:
                d = c.get("data", {})
                ts = float(d.get("created_utc", 0))
                if ts < cutoff_ts:
                    page_ended = True
                    break
                flair = d.get("link_flair_text") or ""
                if require_flair and require_flair.lower() not in flair.lower():
                    continue
                title = html.unescape(d.get("title", ""))
                if self._SEEKER_TITLE_RE.match(title) or self._SEEKER_FLAIR_RE.search(flair):
                    continue
                permalink = d.get("permalink", "")
                yield Item(
                    id=f"reddit:{d['id']}",
                    source_id=source.id,
                    type="job",
                    title=title,
                    url=d.get("url") or f"https://www.reddit.com{permalink}",
                    author=d.get("author", ""),
                    content=html.unescape(d.get("selftext", "")),
                    published_at=ts,
                    metadata={
                        "permalink": permalink,
                        "score": d.get("score", 0),
                        "num_comments": d.get("num_comments", 0),
                        "flair": flair,
                    },
                )
            if page_ended:
                return
            after = data.get("data", {}).get("after")
            if not after:
                return
            time.sleep(self.request_delay)
