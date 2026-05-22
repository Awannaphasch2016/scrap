"""Daily job scraper across multiple sources, scored against Anak's skill profile.

First-pass sources (all public JSON/RSS, no anti-bot):
  - HN "Ask HN: Who is hiring?" — Algolia search API
  - RemoteOK              — https://remoteok.com/api
  - Remotive              — https://remotive.com/api/remote-jobs
  - WeWorkRemotely        — category RSS feed
  - Reddit                — r/forhire (Hiring flair), r/MachineLearningJobs, r/jobbit

Window defaults to last 24h (set JOB_WINDOW_HOURS to override). Each item is
scored by keyword match against SKILL_WEIGHTS; the renderer sorts by score and
drops items below JOB_RELEVANCE_THRESHOLD.

Outputs:
  jobs.db          SQLite store (items table, type='job')
  jobs.html        Sorted feed for browsing
  jobs_digest.md   Markdown bundle suitable for NotebookLM ingestion

Tier-1 sources (Upwork RSS, Wellfound, LinkedIn) plug into the existing
HTTP_PROXY path from lambda/handler.py — wire them via JOB_UPWORK_FEEDS env
when ready.
"""

import dataclasses
import html
import json
import logging
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable

import requests

from curator.core.fetcher import Fetcher, _parse_iso, _parse_rfc822, _proxies, _strip_html
from curator.core.types import Item, Source

logger = logging.getLogger(__name__)

# --------- config ---------

DB_PATH = "jobs.db"
HTML_PATH = "jobs.html"
# Full corpus digest (rotates daily into NotebookLM as the single source).
DIGEST_PATH = "jobs_digest.md"
# Today's date-stamped windowed digest + top-N (published to S3 per-day).
TODAY_PATH_TMPL = "jobs_{date}.md"
TOP_PATH_TMPL = "jobs_top{n}_{date}.md"
WINDOW_HOURS = int(os.environ.get("JOB_WINDOW_HOURS", "36"))
TOP_N = int(os.environ.get("JOB_TOP_N", "10"))
USER_AGENT = "scrap-job-curator/0.1"
REQUEST_DELAY = 1.0
RELEVANCE_THRESHOLD = float(os.environ.get("JOB_RELEVANCE_THRESHOLD", "1.0"))

# Skill keywords → weight, matched case-insensitively against title+content+tags.
# Tuned to Anak's portfolio: AI automation, Meta Ads, scraping, serverless, agents.
SKILL_WEIGHTS: dict[str, float] = {
    # Tier A — direct, high-signal matches
    "web scraping": 4.0, "scraper": 4.0, "scraping": 3.0, "crawler": 3.0,
    "ai agent": 4.0, "agentic": 4.0, "autonomous agent": 4.0,
    "mcp": 4.0, "model context protocol": 4.0,
    "rag": 3.0, "retrieval augmented": 3.0,
    "llm": 3.0, "prompt engineer": 3.0, "anthropic": 3.0, "claude": 2.5, "openai": 2.0,
    "meta ads": 4.0, "facebook ads": 3.0, "google ads": 2.0,
    "ai automation": 4.0, "automation agency": 4.0,
    "n8n": 3.0, "make.com": 3.0, "zapier": 2.0,
    # Tier B — broad skill matches
    "lambda": 2.0, "serverless": 2.0, "aws": 1.5, "terraform": 1.5,
    "python": 2.0, "node.js": 2.0, "nodejs": 2.0,
    "typescript": 1.5, "javascript": 1.0,
    "supabase": 2.0, "postgres": 1.0, "sqlite": 1.0,
    "astro": 2.0, "next.js": 1.5, "nextjs": 1.5, "react": 1.0,
    "cli tool": 2.0, "cli": 1.0, "docker": 1.0,
    "hubspot": 2.0, "crm": 1.5, "cold email": 3.0, "cold outreach": 3.0,
    "lead generation": 2.5, "marketing automation": 3.0,
    "telegram bot": 2.0, "discord bot": 2.0, "slack bot": 2.0, "line bot": 2.0,
    "browser extension": 2.0, "chrome extension": 2.0,
    "video generation": 2.0, "image generation": 2.0,
    # Tier C — adjacent
    "cybersecurity": 1.0, "osint": 2.0,
    "blockchain": 0.5, "smart contract": 0.5,
    "trading": 1.0, "fintech": 1.0, "ticker": 1.0,
}

NEGATIVE_KEYWORDS = ("on-site only", "onsite only", "no remote", "us citizen only")
NEGATIVE_PENALTY = 5.0

SOURCES: list[dict] = [
    {"type": "hn_hiring", "id": "hn:whoishiring", "name": "HN: Who is Hiring",
     "url": "https://news.ycombinator.com/"},
    {"type": "remoteok", "id": "remoteok", "name": "RemoteOK",
     "url": "https://remoteok.com/"},
    {"type": "remotive", "id": "remotive", "name": "Remotive",
     "url": "https://remotive.com/"},
    {"type": "wwr_rss", "id": "wwr:programming", "name": "WeWorkRemotely (Programming)",
     "url": "https://weworkremotely.com/categories/remote-programming-jobs",
     "feed": "https://weworkremotely.com/categories/remote-programming-jobs.rss"},
    {"type": "reddit_jobs", "id": "reddit:forhire", "name": "r/forhire",
     "url": "https://www.reddit.com/r/forhire/",
     "subreddit": "forhire", "require_flair": "Hiring"},
    {"type": "reddit_jobs", "id": "reddit:MLjobs", "name": "r/MachineLearningJobs",
     "url": "https://www.reddit.com/r/MachineLearningJobs/",
     "subreddit": "MachineLearningJobs", "require_flair": None},
    {"type": "reddit_jobs", "id": "reddit:jobbit", "name": "r/jobbit",
     "url": "https://www.reddit.com/r/jobbit/",
     "subreddit": "jobbit", "require_flair": None},
]


# --------- data model ---------
# Source + Item moved to curator.core.types (Stage 1 of curator/core extraction).

# --------- store ---------

class Store:
    def __init__(self, path: str = DB_PATH):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._setup()

    def _setup(self) -> None:
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS sources (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                url TEXT
            );
            CREATE TABLE IF NOT EXISTS items (
                id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                type TEXT NOT NULL,
                title TEXT,
                url TEXT,
                author TEXT,
                content TEXT,
                published_at REAL,
                fetched_at REAL DEFAULT (unixepoch()),
                relevance REAL DEFAULT 0,
                metadata TEXT,
                FOREIGN KEY (source_id) REFERENCES sources(id)
            );
            CREATE INDEX IF NOT EXISTS idx_items_source_published
                ON items(source_id, published_at DESC);
            CREATE INDEX IF NOT EXISTS idx_items_relevance
                ON items(relevance DESC, published_at DESC);
        """)
        self.conn.commit()

    def upsert_source(self, s: Source) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO sources (id, name, type, url) VALUES (?, ?, ?, ?)",
            (s.id, s.name, s.type, s.url),
        )
        self.conn.commit()

    def upsert_item(self, i: Item, relevance: float) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO items
               (id, source_id, type, title, url, author, content,
                published_at, relevance, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                i.id, i.source_id, i.type, i.title, i.url, i.author, i.content,
                i.published_at, relevance,
                json.dumps(i.metadata, ensure_ascii=False),
            ),
        )

    def commit(self) -> None:
        self.conn.commit()

    def list_sources(self) -> list[Source]:
        rows = self.conn.execute(
            "SELECT id, name, type, url FROM sources ORDER BY name"
        ).fetchall()
        return [Source(id=r["id"], name=r["name"], type=r["type"], url=r["url"] or "")
                for r in rows]

    def items_for_source(
        self, source_id: str, since_ts: float, min_relevance: float = 0.0
    ) -> list[tuple[Item, float]]:
        rows = self.conn.execute(
            """SELECT id, source_id, type, title, url, author, content,
                      published_at, relevance, metadata
               FROM items
               WHERE source_id = ? AND published_at >= ? AND relevance >= ?
               ORDER BY relevance DESC, published_at DESC""",
            (source_id, since_ts, min_relevance),
        ).fetchall()
        return [self._row_to_pair(r) for r in rows]

    def all_items(
        self, min_relevance: float = 0.0, since_ts: float | None = None,
    ) -> list[tuple[Item, float, str]]:
        """Return (item, score, source_name) across all sources, sorted by score, recency."""
        params: list = [min_relevance]
        where = "i.relevance >= ?"
        if since_ts is not None:
            where += " AND i.published_at >= ?"
            params.append(since_ts)
        rows = self.conn.execute(
            f"""SELECT i.id, i.source_id, i.type, i.title, i.url, i.author, i.content,
                       i.published_at, i.relevance, i.metadata, s.name AS source_name
                FROM items i JOIN sources s ON s.id = i.source_id
                WHERE {where}
                ORDER BY i.relevance DESC, i.published_at DESC""",
            params,
        ).fetchall()
        out: list[tuple[Item, float, str]] = []
        for r in rows:
            item, score = self._row_to_pair(r)
            out.append((item, score, r["source_name"]))
        return out

    @staticmethod
    def _row_to_pair(r: sqlite3.Row) -> tuple[Item, float]:
        item = Item(
            id=r["id"], source_id=r["source_id"], type=r["type"],
            title=r["title"] or "", url=r["url"] or "",
            author=r["author"] or "", content=r["content"] or "",
            published_at=r["published_at"] or 0,
            metadata=json.loads(r["metadata"]) if r["metadata"] else {},
        )
        return item, (r["relevance"] or 0.0)


# --------- relevance ---------

_KEYWORD_PATTERNS = [
    (re.compile(rf"\b{re.escape(k)}\b", re.IGNORECASE), w)
    for k, w in SKILL_WEIGHTS.items()
]
_NEGATIVE_PATTERNS = [
    re.compile(rf"\b{re.escape(k)}\b", re.IGNORECASE) for k in NEGATIVE_KEYWORDS
]


def score_item(item: Item) -> tuple[float, list[str]]:
    """Return (score, matched_keywords). Title hits weighted 2x, tags 1.5x."""
    title = item.title or ""
    content = item.content or ""
    tags = " ".join(item.metadata.get("tags", []) or []) if isinstance(item.metadata, dict) else ""

    score = 0.0
    matched: list[str] = []
    for pat, weight in _KEYWORD_PATTERNS:
        hits = 0
        if pat.search(title):
            hits += 2  # title hit
        if pat.search(tags):
            hits += 1  # tags hit (worth 1.5x via weight below)
        if pat.search(content):
            hits += 1
        if hits:
            score += weight * hits
            matched.append(pat.pattern.strip("\\b"))

    for pat in _NEGATIVE_PATTERNS:
        if pat.search(title) or pat.search(content):
            score -= NEGATIVE_PENALTY

    return score, matched


# --------- http helpers ---------

# _proxies moved to curator.core.fetcher (Stage 2 of curator/core extraction).


def _get_json(url: str, timeout: int = 20) -> dict | list:
    r = requests.get(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=timeout, proxies=_proxies(),
    )
    r.raise_for_status()
    return r.json()


def _get_text(url: str, timeout: int = 20) -> str:
    r = requests.get(
        url, headers={"User-Agent": USER_AGENT}, timeout=timeout, proxies=_proxies(),
    )
    r.raise_for_status()
    return r.text


# _strip_html moved to curator.core.fetcher.

# --------- fetchers ---------
# Fetcher ABC moved to curator.core.fetcher (Stage 2 of curator/core extraction).


class HNHiringFetcher(Fetcher):
    """Fetch the latest 'Ask HN: Who is hiring?' thread and stream comments as jobs."""

    # search_by_date sorts by created_at desc · we filter by strict regex below to
    # avoid grabbing meta threads like 'Why can't I post on Who is Hiring?'.
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

    def fetch(self, source: Source, cfg: dict, cutoff_ts: float) -> Iterable[Item]:
        data = _get_json(self.SEARCH_URL)
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
        logger.info("hn_hiring: thread %s (%s) — %s", story_id, story_title,
                    datetime.fromtimestamp(story_ts, timezone.utc).strftime("%Y-%m"))

        url = self.COMMENTS_URL_TMPL.format(story_id=story_id, cutoff=int(cutoff_ts))
        comments = _get_json(url)
        for c in comments.get("hits", []) if isinstance(comments, dict) else []:
            body_html = c.get("comment_text") or ""
            body = _strip_html(body_html)
            if not body:
                continue
            # Heuristic: skip pure "I'm looking" / "seeking work" comments — those are
            # opposite-side posts. Real job listings tend to start with company/role.
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


class RemoteOKFetcher(Fetcher):
    URL = "https://remoteok.com/api"

    def fetch(self, source: Source, cfg: dict, cutoff_ts: float) -> Iterable[Item]:
        data = _get_json(self.URL)
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


class RemotiveFetcher(Fetcher):
    URL = "https://remotive.com/api/remote-jobs"

    def fetch(self, source: Source, cfg: dict, cutoff_ts: float) -> Iterable[Item]:
        data = _get_json(self.URL)
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


class WWRRSSFetcher(Fetcher):
    def fetch(self, source: Source, cfg: dict, cutoff_ts: float) -> Iterable[Item]:
        feed_url = cfg.get("feed") or cfg.get("url")
        if not feed_url:
            return
        text = _get_text(feed_url)
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


class RedditJobsFetcher(Fetcher):
    """Reddit subreddit fetcher · filters posts by flair when require_flair is set.

    Also skips seeker-side posts (titles starting with '[for hire]', '[fh]', or
    flagged with a 'For Hire'/'Seeking work' flair) — Anak wants jobs to APPLY
    TO, not other freelancers offering services.
    """

    _SEEKER_TITLE_RE = re.compile(
        r"^\s*\[(?:for[\s-]*hire|fh|seeking|hire\s*me|hireme)\b", re.IGNORECASE
    )
    _SEEKER_FLAIR_RE = re.compile(r"\b(for[\s-]*hire|seeking\s*work|hire\s*me)\b", re.IGNORECASE)

    def fetch(self, source: Source, cfg: dict, cutoff_ts: float) -> Iterable[Item]:
        subreddit = cfg["subreddit"]
        require_flair = cfg.get("require_flair")
        after = None
        while True:
            url = f"https://www.reddit.com/r/{subreddit}/new.json?limit=100"
            if after:
                url += f"&after={after}"
            try:
                data = _get_json(url, timeout=20)
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
                # Seeker-side posts are not jobs we'd apply to — drop them.
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
                        "permalink": permalink, "score": d.get("score", 0),
                        "num_comments": d.get("num_comments", 0),
                        "flair": flair,
                    },
                )
            if page_ended:
                return
            after = data.get("data", {}).get("after")
            if not after:
                return
            time.sleep(REQUEST_DELAY)


FETCHERS: dict[str, Fetcher] = {
    "hn_hiring": HNHiringFetcher(),
    "remoteok": RemoteOKFetcher(),
    "remotive": RemotiveFetcher(),
    "wwr_rss": WWRRSSFetcher(),
    "reddit_jobs": RedditJobsFetcher(),
}


# --------- date parsing ---------

# _parse_iso and _parse_rfc822 moved to curator.core.fetcher.


# --------- renderer ---------

def _humanize_age(ts: float, now_ts: float) -> str:
    delta = max(0.0, now_ts - ts)
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    return f"{int(delta / 86400)}d ago"


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Jobs — last {window_hours}h</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    max-width: 880px;
    margin: 2rem auto;
    padding: 0 1rem;
    line-height: 1.55;
  }}
  header {{ border-bottom: 1px solid #8884; padding-bottom: 0.75rem; margin-bottom: 1.5rem; }}
  header h1 {{ margin: 0; font-size: 1.4rem; }}
  header .meta {{ color: #888; font-size: 0.85rem; margin-top: 0.25rem; }}
  section {{ margin-bottom: 2rem; }}
  section h2 {{
    font-size: 1.05rem;
    margin: 0 0 0.5rem 0;
    padding-bottom: 0.25rem;
    border-bottom: 1px solid #8882;
  }}
  section h2 a {{ color: inherit; text-decoration: none; }}
  section h2 .count {{ color: #888; font-weight: normal; font-size: 0.85rem; }}
  ul.jobs {{ list-style: none; padding: 0; margin: 0; }}
  ul.jobs li {{ margin: 0.5rem 0; padding: 0.4rem 0.6rem; border-left: 3px solid #8883; }}
  ul.jobs li.hi {{ border-left-color: #2ecc71; }}
  ul.jobs li.mid {{ border-left-color: #f1c40f; }}
  ul.jobs a.title {{ font-weight: 500; }}
  ul.jobs .meta {{ color: #888; font-size: 0.82rem; margin-top: 0.15rem; }}
  ul.jobs .matched {{ color: #2ecc71; font-size: 0.78rem; margin-top: 0.15rem; }}
  ul.jobs .score {{ font-weight: 600; color: #2ecc71; margin-right: 0.4rem; }}
</style>
</head>
<body>
<header>
  <h1>Jobs — last {window_hours}h</h1>
  <div class="meta">Generated {generated_at} · threshold ≥ {threshold} · {total} listings</div>
</header>
{sections}
</body>
</html>
"""


class Renderer:
    def render(
        self, store: Store, out_path: str = HTML_PATH,
        window_hours: int = WINDOW_HOURS, threshold: float = RELEVANCE_THRESHOLD,
    ) -> int:
        now = datetime.now(timezone.utc)
        cutoff_ts = (now - timedelta(hours=window_hours)).timestamp()
        sections: list[str] = []
        total = 0
        for source in store.list_sources():
            scored = store.items_for_source(source.id, cutoff_ts, min_relevance=threshold)
            total += len(scored)
            items_html: list[str] = []
            for item, score in scored:
                cls = "hi" if score >= 6 else ("mid" if score >= 3 else "")
                age = _humanize_age(item.published_at, now.timestamp())
                tags = item.metadata.get("tags") or []
                tags_str = ", ".join(tags[:6]) if tags else ""
                _, matched = score_item(item)
                matched_str = ", ".join(sorted(set(matched))[:8])
                items_html.append(
                    f'<li class="{cls}">'
                    f'<span class="score">{score:.1f}</span>'
                    f'<a class="title" href="{html.escape(item.url)}">{html.escape(item.title)}</a>'
                    f'<div class="meta">{age}'
                    + (f' · tags: {html.escape(tags_str)}' if tags_str else "")
                    + (f' · u/{html.escape(item.author)}' if item.author else "")
                    + "</div>"
                    + (f'<div class="matched">↳ {html.escape(matched_str)}</div>' if matched_str else "")
                    + "</li>"
                )
            header_link = (
                f'<a href="{html.escape(source.url)}">{html.escape(source.name)}</a>'
                if source.url else html.escape(source.name)
            )
            sections.append(
                f'<section>\n'
                f'  <h2>{header_link} <span class="count">({len(scored)})</span></h2>\n'
                f'  <ul class="jobs">\n'
                + "\n".join(f"    {i}" for i in items_html)
                + "\n  </ul>\n</section>"
            )

        Path(out_path).write_text(
            HTML_TEMPLATE.format(
                generated_at=now.strftime("%Y-%m-%d %H:%M UTC"),
                window_hours=window_hours,
                threshold=f"{threshold:g}",
                total=total,
                sections="\n".join(sections),
            ),
            encoding="utf-8",
        )
        return total


# --------- bundler ---------

class Bundler:
    """Markdown digests for NotebookLM ingestion + per-day S3 archives.

    Emits three artifacts per run:
      1. DIGEST_PATH        — full corpus (all items in DB), no window filter.
                              Rotated daily into NotebookLM as the single source
                              so `jobs ask` can ground on every curated job.
      2. jobs_{date}.md     — today's windowed digest (date-stamped).
      3. jobs_top{N}_{date}.md — today's top-N (date-stamped).
    """

    def export_full_corpus(
        self, store: Store, out_path: str = DIGEST_PATH,
        threshold: float = RELEVANCE_THRESHOLD,
    ) -> int:
        scored = store.all_items(min_relevance=threshold)
        now = datetime.now(timezone.utc)
        header = (
            f"# Jobs Corpus — all curated listings\n"
            f"\n"
            f"Generated: {now.strftime('%Y-%m-%d %H:%M UTC')} · "
            f"threshold ≥ {threshold:g} · {len(scored)} listings\n"
            f"\n"
        )
        Path(out_path).write_text(header + _render_items_md(scored), encoding="utf-8")
        return len(scored)

    def export_dated(
        self, store: Store, today: datetime, out_path: str,
        window_hours: int = WINDOW_HOURS, threshold: float = RELEVANCE_THRESHOLD,
        top_n: int | None = None,
    ) -> int:
        cutoff_ts = (today - timedelta(hours=window_hours)).timestamp()
        scored = store.all_items(min_relevance=threshold, since_ts=cutoff_ts)
        if top_n is not None:
            scored = scored[:top_n]
        date_str = today.strftime("%Y-%m-%d")
        title = (
            f"# Jobs — top {top_n} for {date_str}"
            if top_n is not None else f"# Jobs — {date_str} (last {window_hours}h)"
        )
        header = (
            f"{title}\n\n"
            f"Generated: {today.strftime('%Y-%m-%d %H:%M UTC')} · "
            f"threshold ≥ {threshold:g} · {len(scored)} listings\n\n"
        )
        Path(out_path).write_text(header + _render_items_md(scored), encoding="utf-8")
        return len(scored)


def _render_items_md(scored: list[tuple[Item, float, str]]) -> str:
    lines: list[str] = []
    for item, score, source_name in scored:
        pub = datetime.fromtimestamp(item.published_at, timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
        _, matched = score_item(item)
        matched_str = ", ".join(sorted(set(matched))[:10]) or "—"
        tags = item.metadata.get("tags") or []
        tags_str = ", ".join(tags[:8]) if tags else ""
        lines.append(f"### [{score:.1f}] {item.title}")
        lines.append("")
        meta_bits = [f"Source: {source_name}", f"Posted: {pub}"]
        if item.author:
            meta_bits.append(f"By: {item.author}")
        if tags_str:
            meta_bits.append(f"Tags: {tags_str}")
        lines.append(" · ".join(meta_bits))
        lines.append("")
        lines.append(f"Link: {item.url}")
        lines.append(f"Matched: {matched_str}")
        lines.append("")
        body = (item.content or "").strip()
        if body:
            if len(body) > 1500:
                body = body[:1500] + "…"
            lines.append(body)
            lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)


# --------- NotebookLM upload + S3 publish (shared shape with news_curator) ---------

# Reuse news_curator's NotebookLMUploader machinery (storage_state + Doppler pull/push)
# verbatim · jobs only differ by the notebook id and the digest file we hand it.
def _upload_to_notebooklm(file_path: str, notebook_id: str) -> None:
    from news_curator import NotebookLMUploader
    NotebookLMUploader(notebook_id).upload(file_path)


def _publish_to_s3(local_path: str, bucket: str, key: str) -> None:
    import boto3
    s3 = boto3.client("s3")
    body = Path(local_path).read_bytes()
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="text/markdown")
    logger.info("s3: published %s → s3://%s/%s (%d bytes)", local_path, bucket, key, len(body))


# --------- pipeline ---------

def _source_from_cfg(cfg: dict) -> Source:
    return Source(id=cfg["id"], name=cfg["name"], type=cfg["type"], url=cfg.get("url", ""))


def main() -> None:
    print(f"Curating jobs (last {WINDOW_HOURS}h, threshold ≥ {RELEVANCE_THRESHOLD:g})...")
    store = Store()
    now = datetime.now(timezone.utc)
    cutoff_ts = (now - timedelta(hours=WINDOW_HOURS)).timestamp()

    total_fetched = 0
    total_kept = 0
    for cfg in SOURCES:
        source = _source_from_cfg(cfg)
        store.upsert_source(source)
        fetcher = FETCHERS.get(source.type)
        if not fetcher:
            print(f"[{source.name}] unknown source type: {source.type}; skipping")
            continue
        print(f"\n[{source.name}]")
        n_fetched = n_kept = 0
        try:
            for item in fetcher.fetch(source, cfg, cutoff_ts):
                n_fetched += 1
                score, _ = score_item(item)
                store.upsert_item(item, score)
                if score >= RELEVANCE_THRESHOLD:
                    n_kept += 1
            store.commit()
        except Exception as e:
            print(f"  fetcher error: {e}")
            logger.exception("fetcher %s failed", source.id)
            continue
        print(f"  fetched: {n_fetched}, kept (score ≥ {RELEVANCE_THRESHOLD:g}): {n_kept}")
        total_fetched += n_fetched
        total_kept += n_kept
        time.sleep(REQUEST_DELAY)

    print(f"\nTotals: {total_fetched} fetched, {total_kept} kept")

    print("\nRendering HTML...")
    n_html = Renderer().render(store)
    print(f"  wrote {HTML_PATH} ({n_html} listings)")

    print("\nBundling markdown digests...")
    bundler = Bundler()
    date_str = now.strftime("%Y-%m-%d")
    today_path = TODAY_PATH_TMPL.format(date=date_str)
    top_path = TOP_PATH_TMPL.format(n=TOP_N, date=date_str)

    n_corpus = bundler.export_full_corpus(store)
    print(f"  wrote {DIGEST_PATH} (full corpus: {n_corpus})")

    n_today = bundler.export_dated(store, now, today_path)
    print(f"  wrote {today_path} (today: {n_today})")

    n_top = bundler.export_dated(store, now, top_path, top_n=TOP_N)
    print(f"  wrote {top_path} (top {TOP_N}: {n_top})")

    # NotebookLM: rotate the single corpus source so `jobs ask` sees today's data.
    notebook_id = os.environ.get("JOBS_NOTEBOOK_ID")
    if notebook_id:
        print(f"\nUploading corpus to NotebookLM (notebook {notebook_id})...")
        try:
            _upload_to_notebooklm(DIGEST_PATH, notebook_id)
            print("  upload complete")
        except ImportError:
            print("  notebooklm-py not installed; pip install 'notebooklm-py[browser]'")
        except Exception as e:
            print(f"  upload failed: {e}")
            logger.exception("notebooklm upload failed")
    else:
        print("\n(set JOBS_NOTEBOOK_ID to upload corpus to NotebookLM)")

    # S3 publish: dated digest + dated top-N to s3://JOBS_FEED_BUCKET/jobs/...
    bucket = os.environ.get("JOBS_FEED_BUCKET") or os.environ.get("FEED_BUCKET")
    if bucket:
        print(f"\nPublishing to s3://{bucket}/jobs/...")
        for local, key in (
            (today_path, f"jobs/{date_str}.md"),
            (top_path, f"jobs/{date_str}-top{TOP_N}.md"),
        ):
            try:
                _publish_to_s3(local, bucket, key)
                print(f"  s3://{bucket}/{key}")
            except Exception as e:
                print(f"  publish failed for {key}: {e}")
                logger.exception("s3 publish failed for %s", key)
    else:
        print("\n(set JOBS_FEED_BUCKET to publish per-day files to S3)")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
