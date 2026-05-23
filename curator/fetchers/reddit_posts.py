"""Reddit subreddit fetcher · posts + top-level comments.

News-shape fetcher · used by the scraping topic (r/WebScraping) to capture
both the link/text post AND its first level of discussion. Jobs uses a
separate reddit_jobs.RedditJobsFetcher that filters by flair and skips
comments (job listings don't usually need discussion).

Egress respects HTTP_PROXY / HTTPS_PROXY via curator.core.fetcher._proxies
so Lambda's Tailscale → laptop tinyproxy chain is used automatically.
"""

from __future__ import annotations

import html
import time
from typing import Iterable

import requests

from curator.core.fetcher import DEFAULT_USER_AGENT, Fetcher, _proxies
from curator.core.types import Item, Source


class RedditFetcher(Fetcher):
    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        request_delay: float = 1.5,
    ) -> None:
        self.user_agent = user_agent
        self.request_delay = request_delay

    def fetch(self, source: Source, source_cfg: dict, cutoff_ts: float) -> Iterable[Item]:
        subreddit = source_cfg["subreddit"]
        for post in self._fetch_posts(subreddit, cutoff_ts):
            yield self._post_to_item(post, source.id)
            for c in self._fetch_top_level_comments(post["permalink"]):
                yield self._comment_to_item(c, source.id, post["id"])
            time.sleep(self.request_delay)

    def _fetch_posts(self, subreddit: str, cutoff_ts: float) -> list[dict]:
        posts: list[dict] = []
        after = None
        headers = {"User-Agent": self.user_agent}
        while True:
            url = f"https://www.reddit.com/r/{subreddit}/new.json?limit=100"
            if after:
                url += f"&after={after}"
            r = requests.get(url, headers=headers, timeout=15, proxies=_proxies())
            r.raise_for_status()
            data = r.json()
            children = data["data"]["children"]
            if not children:
                break
            page_ended = False
            for c in children:
                d = c["data"]
                if d["created_utc"] < cutoff_ts:
                    page_ended = True
                    break
                posts.append(d)
            if page_ended:
                break
            after = data["data"].get("after")
            if not after:
                break
            time.sleep(self.request_delay)
        return posts

    def _fetch_top_level_comments(self, permalink: str) -> list[dict]:
        url = f"https://www.reddit.com{permalink}.json?limit=200&depth=1"
        headers = {"User-Agent": self.user_agent}
        try:
            r = requests.get(url, headers=headers, timeout=15, proxies=_proxies())
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"  comment fetch failed for {permalink}: {e}")
            return []
        listings = r.json()
        if len(listings) < 2:
            return []
        out = []
        for c in listings[1]["data"]["children"]:
            if c["kind"] != "t1":
                continue
            out.append(c["data"])
        return out

    def _post_to_item(self, p: dict, source_id: str) -> Item:
        permalink = p.get("permalink", "")
        return Item(
            id=f"reddit:{p['id']}",
            source_id=source_id,
            type="post",
            title=html.unescape(p.get("title", "")),
            url=p.get("url") or f"https://www.reddit.com{permalink}",
            author=p.get("author", ""),
            content=html.unescape(p.get("selftext", "")),
            published_at=p.get("created_utc", 0),
            metadata={
                "permalink": permalink,
                "score": p.get("score", 0),
                "num_comments": p.get("num_comments", 0),
            },
        )

    def _comment_to_item(self, c: dict, source_id: str, parent_post_id: str) -> Item:
        body = html.unescape(c.get("body", ""))
        author = c.get("author", "")
        # Comments aren't shown as their own cards in the curator UI (they're
        # filtered out client-side · see web/src/pages/index.astro VIEWS).
        # Title here is just an honest identifier for the digest / NotebookLM
        # pipelines, which DO render comments grouped under their parent post.
        first_line = body.split("\n", 1)[0].strip()
        title = first_line[:120] if first_line else f"comment by {author or 'unknown'}"
        return Item(
            id=f"reddit:{c['id']}",
            source_id=source_id,
            type="comment",
            title=title,
            url=f"https://www.reddit.com/comments/{parent_post_id}/_/{c['id']}/",
            author=author,
            content=body,
            published_at=c.get("created_utc", 0),
            metadata={
                "score": c.get("score", 0),
                "parent_post_id": f"reddit:{parent_post_id}",
            },
        )
