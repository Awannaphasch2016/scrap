"""Jobs topic config · multi-source job feed scored against Anak's skills.

Topic = `jobs`. Pulls from HN "Who is hiring?", RemoteOK, Remotive,
WeWorkRemotely, and 3 Reddit subs. Each item is scored by keyword match
against SKILL_WEIGHTS; the Bundler writes three artifacts per run:

  1. jobs_digest.md             — full corpus (rotates into NotebookLM)
  2. jobs_{YYYY-MM-DD}.md       — today's windowed digest (S3-published)
  3. jobs_top{N}_{YYYY-MM-DD}.md — today's top-N (S3-published)

The post-upload hook publishes (2) and (3) under s3://<bucket>/jobs/...
so older days can be scrolled.
"""

from __future__ import annotations

import html
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from curator.core.pipeline import run
from curator.core.render import _humanize_age
from curator.core.s3 import publish_to_s3
from curator.core.store import Store
from curator.core.topic_config import TopicConfig
from curator.core.types import Item
from curator.fetchers.hn_hiring import HNHiringFetcher
from curator.fetchers.reddit_jobs import RedditJobsFetcher
from curator.fetchers.remoteok import RemoteOKFetcher
from curator.fetchers.remotive import RemotiveFetcher
from curator.fetchers.wwr_rss import WWRRSSFetcher

logger = logging.getLogger(__name__)

# --------- constants ---------

DB_PATH = "jobs.db"
HTML_PATH = "jobs.html"
DIGEST_PATH = "jobs_digest.md"              # full corpus · uploaded to NotebookLM
TODAY_PATH_TMPL = "jobs_{date}.md"          # today's windowed digest
TOP_PATH_TMPL = "jobs_top{n}_{date}.md"     # today's top-N

WINDOW_HOURS = int(os.environ.get("JOB_WINDOW_HOURS", "36"))
WINDOW_SECONDS = WINDOW_HOURS * 3600.0
TOP_N = int(os.environ.get("JOB_TOP_N", "10"))
USER_AGENT = "scrap-job-curator/0.1"
REQUEST_DELAY = 1.0
RELEVANCE_THRESHOLD = float(os.environ.get("JOB_RELEVANCE_THRESHOLD", "1.0"))

# Skill keywords → weight. Tuned to Anak's portfolio.
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

FETCHERS = {
    "hn_hiring":   HNHiringFetcher(user_agent=USER_AGENT),
    "remoteok":    RemoteOKFetcher(user_agent=USER_AGENT),
    "remotive":    RemotiveFetcher(user_agent=USER_AGENT),
    "wwr_rss":     WWRRSSFetcher(user_agent=USER_AGENT),
    "reddit_jobs": RedditJobsFetcher(user_agent=USER_AGENT, request_delay=REQUEST_DELAY),
}


# --------- relevance ---------

_KEYWORD_PATTERNS = [
    (re.compile(rf"\b{re.escape(k)}\b", re.IGNORECASE), w)
    for k, w in SKILL_WEIGHTS.items()
]
_NEGATIVE_PATTERNS = [
    re.compile(rf"\b{re.escape(k)}\b", re.IGNORECASE) for k in NEGATIVE_KEYWORDS
]


def score_item(item: Item) -> tuple[float, list[str]]:
    """Return (score, matched_keywords). Title hits weighted 2x, tags 1x."""
    title = item.title or ""
    content = item.content or ""
    tags = (
        " ".join(item.metadata.get("tags", []) or [])
        if isinstance(item.metadata, dict) else ""
    )

    score = 0.0
    matched: list[str] = []
    for pat, weight in _KEYWORD_PATTERNS:
        hits = 0
        if pat.search(title):
            hits += 2
        if pat.search(tags):
            hits += 1
        if pat.search(content):
            hits += 1
        if hits:
            score += weight * hits
            matched.append(pat.pattern.strip("\\b"))

    for pat in _NEGATIVE_PATTERNS:
        if pat.search(title) or pat.search(content):
            score -= NEGATIVE_PENALTY

    return score, matched


# --------- date-stamped artifact paths · derived from "now" ---------

def _dated_paths(now: datetime) -> tuple[str, str]:
    """Return (today_windowed_path, today_topn_path) for the given UTC now."""
    date_str = now.strftime("%Y-%m-%d")
    return (
        TODAY_PATH_TMPL.format(date=date_str),
        TOP_PATH_TMPL.format(n=TOP_N, date=date_str),
    )


# --------- renderer ---------

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
    """Jobs-topic renderer · per-source sections, scored & matched-keyword-tagged."""

    def __init__(self, cfg: TopicConfig) -> None:
        self.cfg = cfg

    def render(self, store: Store, out_path: str) -> int:
        now = datetime.now(timezone.utc)
        cutoff_ts = (now - timedelta(seconds=self.cfg.window_seconds)).timestamp()
        threshold = self.cfg.relevance_threshold
        window_hours = int(self.cfg.window_seconds / 3600)

        sections: list[str] = []
        total = 0
        for source in store.list_sources():
            scored = store.items_for_source(
                source.id, cutoff_ts, min_relevance=threshold,
            )
            # Chronological within each source · matches the daily reader's
            # mental model ("what landed today, newest first"). Relevance
            # score is still displayed on each row as the colored chip.
            scored.sort(key=lambda pair: -pair[0].published_at)
            total += len(scored)
            items_html: list[str] = []
            for item, score in scored:
                score = score or 0.0
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

def _render_items_md(scored: list[tuple[Item, float | None, str]]) -> str:
    lines: list[str] = []
    for item, score, source_name in scored:
        score = score or 0.0
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


def _sources_header(store: Store, cutoff_ts: float, threshold: float) -> str:
    """List each polled source with its URL and how many items it contributed
    within the window. Renders as a markdown bullet list so the daily digest
    is self-describing.

    Trailing `\\n\\n` ensures a blank line between this bullet list and
    whatever the caller concatenates next (typically the first `###` item
    heading) so markdown renderers don't run the bullet into the heading.
    """
    lines: list[str] = ["**Sources polled:**", ""]
    for source in store.list_sources():
        items = store.items_for_source(
            source.id, cutoff_ts, min_relevance=threshold,
        )
        url = source.url or ""
        link = f"[{source.name}]({url})" if url else source.name
        n = len(items)
        lines.append(f"- {link} · `{source.type}` · {n} item{'s' if n != 1 else ''} kept")
    return "\n".join(lines) + "\n\n"


class Bundler:
    """Writes the rotating full-corpus digest AND today's dated/top-N artifacts.

    The pipeline calls `export(store, out_path)` once · this writes all three
    files. The post-upload hook then S3-publishes the dated ones (paths
    re-derived from the same `_dated_paths(now)`).

    Sort policy:
      - jobs_digest.md          · NotebookLM consumer · sort by relevance DESC
      - jobs_YYYY-MM-DD.md      · daily reader view  · sort CHRONOLOGICAL
                                  (published_at DESC, most-recent-first), so
                                  the page reads like a feed of "what landed
                                  today" rather than a leaderboard
      - jobs_topN_YYYY-MM-DD.md · "best matches"     · sort by relevance DESC
    """

    def __init__(self, cfg: TopicConfig) -> None:
        self.cfg = cfg

    def export(self, store: Store, out_path: str) -> int:
        threshold = self.cfg.relevance_threshold
        now = datetime.now(timezone.utc)
        window_hours = int(self.cfg.window_seconds / 3600)
        cutoff_ts = (now - timedelta(seconds=self.cfg.window_seconds)).timestamp()

        # 1. Full corpus → out_path (cfg.digest_path) · NotebookLM source.
        # Sort by relevance DESC so the LLM sees the highest-value items first.
        scored_all = store.all_items(min_relevance=threshold)
        corpus_header = (
            f"# Jobs Corpus — all curated listings\n\n"
            f"Generated: {now:%Y-%m-%d %H:%M UTC} · threshold ≥ {threshold:g} · "
            f"{len(scored_all)} listings\n\n"
            + _sources_header(store, 0.0, threshold)
        )
        Path(out_path).write_text(
            corpus_header + _render_items_md(scored_all), encoding="utf-8"
        )
        print(f"  full corpus: {len(scored_all)} listings → {out_path}")

        # 2. Today's windowed digest · CHRONOLOGICAL (most-recent-first).
        today_path, top_path = _dated_paths(now)
        scored_today_relevance = store.all_items(
            min_relevance=threshold, since_ts=cutoff_ts,
        )
        scored_today_chrono = sorted(
            scored_today_relevance, key=lambda t: -t[0].published_at,
        )
        today_header = (
            f"# Jobs — {now:%Y-%m-%d} (last {window_hours}h, most-recent-first)\n\n"
            f"Generated: {now:%Y-%m-%d %H:%M UTC} · threshold ≥ {threshold:g} · "
            f"{len(scored_today_chrono)} listings\n\n"
            + _sources_header(store, cutoff_ts, threshold)
        )
        Path(today_path).write_text(
            today_header + _render_items_md(scored_today_chrono), encoding="utf-8",
        )
        print(f"  today's digest (chronological): {len(scored_today_chrono)} → {today_path}")

        # 3. Top-N · stays RELEVANCE-RANKED (this is the "best matches" view).
        top_scored = scored_today_relevance[:TOP_N]
        top_header = (
            f"# Jobs — top {TOP_N} for {now:%Y-%m-%d} (by relevance score)\n\n"
            f"Generated: {now:%Y-%m-%d %H:%M UTC} · threshold ≥ {threshold:g} · "
            f"{len(top_scored)} listings\n\n"
            + _sources_header(store, cutoff_ts, threshold)
        )
        Path(top_path).write_text(
            top_header + _render_items_md(top_scored), encoding="utf-8",
        )
        print(f"  top-{TOP_N} (by relevance): {len(top_scored)} → {top_path}")
        return len(scored_all)


# --------- post-upload hook · publishes the dated/top-N artifacts to S3 ---------

def _post_upload(
    cfg: TopicConfig, notebook_id: str | None, bucket: str | None, upload_ok: bool
) -> None:
    if not bucket:
        return
    now = datetime.now(timezone.utc)
    today_path, top_path = _dated_paths(now)
    date_str = now.strftime("%Y-%m-%d")
    prefix = cfg.effective_s3_prefix

    print(f"\nPublishing to s3://{bucket}/{prefix}/...")
    # Three artifacts per day · the dated markdown (today's items,
    # chronological), the relevance-ranked top-N markdown, and the HTML
    # rendering of today's items (chronological, color-coded). The HTML
    # is the bookmarkable daily-reader view; the markdowns are the
    # NotebookLM-ingestible / scriptable view.
    for local, key in (
        (today_path,     f"{prefix}/{date_str}.md"),
        (top_path,       f"{prefix}/{date_str}-top{TOP_N}.md"),
        (cfg.html_path,  f"{prefix}/{date_str}.html"),
    ):
        try:
            publish_to_s3(local, bucket, key)
            print(f"  s3://{bucket}/{key}")
        except Exception as e:  # noqa: BLE001
            print(f"  publish failed for {key}: {e}")
            logger.exception("s3 publish failed for %s", key)


# --------- topic config + entry point ---------

TOPIC = TopicConfig(
    name="jobs",
    db_path=DB_PATH,
    html_path=HTML_PATH,
    digest_path=DIGEST_PATH,
    sources=SOURCES,
    fetchers=FETCHERS,
    window_seconds=WINDOW_SECONDS,
    user_agent=USER_AGENT,
    notebook_env_var="JOBS_NOTEBOOK_ID",
    feed_bucket_env_var="JOBS_FEED_BUCKET",
    s3_key_prefix="jobs",
    score_item=score_item,
    relevance_threshold=RELEVANCE_THRESHOLD,
    renderer_factory=Renderer,
    bundler_factory=Bundler,
    post_upload_hook=_post_upload,
)


def main() -> None:
    run(TOPIC)


__all__ = ["main", "TOPIC", "score_item", "SKILL_WEIGHTS"]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
