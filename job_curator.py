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
from curator.core.notebooklm import NotebookLMUploader
from curator.core.render import _humanize_age
from curator.core.s3 import publish_to_s3
from curator.core.store import Store
from curator.core.types import Item, Source
from curator.fetchers.hn_hiring import HNHiringFetcher
from curator.fetchers.reddit_jobs import RedditJobsFetcher
from curator.fetchers.remoteok import RemoteOKFetcher
from curator.fetchers.remotive import RemotiveFetcher
from curator.fetchers.wwr_rss import WWRRSSFetcher

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
# Store moved to curator.core.store (Stage 3 of curator/core extraction).
# Jobs needs no legacy migration — jobs.db only ever had the unified schema.


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
# _proxies · _get_json · _get_text · _strip_html all moved to
# curator.core.fetcher (Stages 2 + 6 of curator/core extraction).

# --------- fetchers ---------
# All concrete fetchers (HNHiring · RemoteOK · Remotive · WWRRSS · RedditJobs)
# moved to curator.fetchers.* (Stage 6). The FETCHERS registry below wires
# the jobs topic's USER_AGENT + REQUEST_DELAY into each instance.



FETCHERS: dict[str, Fetcher] = {
    "hn_hiring":   HNHiringFetcher(user_agent=USER_AGENT),
    "remoteok":    RemoteOKFetcher(user_agent=USER_AGENT),
    "remotive":    RemotiveFetcher(user_agent=USER_AGENT),
    "wwr_rss":     WWRRSSFetcher(user_agent=USER_AGENT),
    "reddit_jobs": RedditJobsFetcher(user_agent=USER_AGENT, request_delay=REQUEST_DELAY),
}


# --------- date parsing ---------

# _parse_iso and _parse_rfc822 moved to curator.core.fetcher.


# --------- renderer ---------

# _humanize_age moved to curator.core.render (Stage 5 of curator/core extraction).


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


# --------- NotebookLM upload + S3 publish ---------
# NotebookLMUploader imported from curator.core.notebooklm (Stage 4).
# publish_to_s3 imported from curator.core.s3 (Stage 4).


# --------- pipeline ---------

def _source_from_cfg(cfg: dict) -> Source:
    return Source(id=cfg["id"], name=cfg["name"], type=cfg["type"], url=cfg.get("url", ""))


def main() -> None:
    print(f"Curating jobs (last {WINDOW_HOURS}h, threshold ≥ {RELEVANCE_THRESHOLD:g})...")
    store = Store(DB_PATH)
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
            NotebookLMUploader(notebook_id).upload(DIGEST_PATH)
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
                publish_to_s3(local, bucket, key)
                print(f"  s3://{bucket}/{key}")
            except Exception as e:
                print(f"  publish failed for {key}: {e}")
                logger.exception("s3 publish failed for %s", key)
    else:
        print("\n(set JOBS_FEED_BUCKET to publish per-day files to S3)")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
