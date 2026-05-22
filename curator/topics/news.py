"""News topic config · scraping-themed Reddit posts + comments.

Topic = `scraping`. Pulls posts + top-level comments from r/WebScraping,
bundles them into a single rotating digest for NotebookLM, then queries the
notebook for the top-5 items and publishes that derived feed to S3 (so
curator's laptop side can read it without hitting NotebookLM directly).
"""

from __future__ import annotations

import html
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from curator.core.notebooklm import NotebookLMUploader
from curator.core.pipeline import run
from curator.core.render import _humanize_age
from curator.core.store import Store
from curator.core.topic_config import TopicConfig
from curator.core.types import Item
from curator.fetchers.reddit_posts import RedditFetcher

logger = logging.getLogger(__name__)

# --------- constants ---------

DB_PATH = "news.db"
HTML_PATH = "news.html"
DIGEST_PATH = "digest.md"
WINDOW_DAYS = 1
WINDOW_SECONDS = WINDOW_DAYS * 86400.0
USER_AGENT = "scrap-news-curator/0.2"
REQUEST_DELAY = 1.5

FEED_TOPIC = os.environ.get("FEED_TOPIC", "scraping")
FEED_TOPIC_NAME = os.environ.get("FEED_TOPIC_NAME", "Scraping")
NOTEBOOKLM_URL_TEMPLATE = "https://notebooklm.google.com/notebook/{notebook_id}"

SOURCES = [
    {
        "type": "reddit",
        "id": "reddit:WebScraping",
        "name": "r/WebScraping",
        "url": "https://www.reddit.com/r/WebScraping/",
        "subreddit": "WebScraping",
    },
]

FETCHERS = {
    "reddit": RedditFetcher(user_agent=USER_AGENT, request_delay=REQUEST_DELAY),
}


# --------- schema migration · news-only legacy posts/comments → items ---------

def _migrate_legacy_reddit(conn: sqlite3.Connection) -> None:
    """One-time migration · old posts/comments tables → unified items table.

    News-specific · jobs never had the legacy schema. Idempotent · drops
    the legacy tables after a successful copy so subsequent runs short-
    circuit at the existence check.
    """
    cur = conn.cursor()
    existing = {
        r[0]
        for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "posts" not in existing and "comments" not in existing:
        return

    n_items = cur.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    n_posts = (
        cur.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        if "posts" in existing
        else 0
    )
    if n_items >= n_posts and n_posts > 0:
        cur.executescript("DROP TABLE IF EXISTS comments; DROP TABLE IF EXISTS posts;")
        conn.commit()
        return

    print(f"Migrating legacy schema → items ({n_posts} posts)...")
    cur.executescript("""
        INSERT OR REPLACE INTO items
            (id, source_id, type, title, url, author, content, published_at, metadata)
        SELECT
            id, source_id, 'post',
            title,
            COALESCE(NULLIF(url, ''), 'https://www.reddit.com' || permalink),
            author,
            COALESCE(selftext, ''),
            created_utc,
            json_object('permalink', permalink, 'score', score, 'num_comments', num_comments)
        FROM posts;

        INSERT OR REPLACE INTO items
            (id, source_id, type, title, url, author, content, published_at, metadata)
        SELECT
            c.id, p.source_id, 'comment',
            '', '', c.author, c.body, c.created_utc,
            json_object('score', c.score, 'parent_post_id', c.post_id)
        FROM comments c JOIN posts p ON c.post_id = p.id;

        DROP TABLE comments;
        DROP TABLE posts;
    """)
    conn.commit()
    migrated = cur.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    print(f"  migrated to {migrated} items")


# --------- renderer ---------

HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Scraping News</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    max-width: 780px;
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
  ul.posts {{ list-style: none; padding: 0; margin: 0; }}
  ul.posts li {{ margin: 0.5rem 0; }}
  ul.posts a.title {{ font-weight: 500; }}
  ul.posts .meta {{ color: #888; font-size: 0.82rem; margin-left: 0.25rem; }}
  ul.posts .meta a {{ color: inherit; }}
</style>
</head>
<body>
<header>
  <h1>Scraping News</h1>
  <div class="meta">Generated {generated_at} · window: last {window_days} days</div>
</header>
{sections}
</body>
</html>
"""


class Renderer:
    """News-topic renderer · posts grouped by source, sorted by recency."""

    def __init__(self, cfg: TopicConfig) -> None:
        self.cfg = cfg

    def render(self, store: Store, out_path: str) -> None:
        now = datetime.now(timezone.utc)
        cutoff_ts = (now - timedelta(seconds=self.cfg.window_seconds)).timestamp()

        sections: list[str] = []
        for source in store.list_sources():
            posts = store.items_for_source(source.id, cutoff_ts, item_type="post")
            items_html: list[str] = []
            for p, _ in posts:
                age = _humanize_age(p.published_at, now.timestamp())
                permalink = p.metadata.get("permalink", "")
                discussion = ""
                if permalink and p.url and not p.url.startswith("https://www.reddit.com"):
                    discussion = (
                        f' · <a href="https://www.reddit.com{html.escape(permalink)}">discussion</a>'
                    )
                score = p.metadata.get("score", 0)
                ncom = p.metadata.get("num_comments", 0)
                items_html.append(
                    f'<li><a class="title" href="{html.escape(p.url)}">{html.escape(p.title)}</a>'
                    f'<span class="meta"> · ↑{score} · 💬{ncom} · {age}{discussion}</span></li>'
                )

            header_link = (
                f'<a href="{html.escape(source.url)}">{html.escape(source.name)}</a>'
                if source.url
                else html.escape(source.name)
            )
            sections.append(
                f'<section>\n'
                f'  <h2>{header_link} <span class="count">({len(posts)})</span></h2>\n'
                f'  <ul class="posts">\n'
                + "\n".join(f"    {i}" for i in items_html)
                + "\n  </ul>\n</section>"
            )

        Path(out_path).write_text(
            HTML_TEMPLATE.format(
                generated_at=now.strftime("%Y-%m-%d %H:%M UTC"),
                window_days=int(self.cfg.window_seconds / 86400),
                sections="\n".join(sections),
            ),
            encoding="utf-8",
        )


# --------- bundler ---------

class Bundler:
    """News-topic bundler · single digest.md with embedded top comments per post."""

    def __init__(self, cfg: TopicConfig) -> None:
        self.cfg = cfg

    def export(self, store: Store, out_path: str) -> int:
        now = datetime.now(timezone.utc)
        cutoff_ts = (now - timedelta(seconds=self.cfg.window_seconds)).timestamp()
        window_days = int(self.cfg.window_seconds / 86400)

        lines: list[str] = [
            "# Scraping News Digest",
            "",
            f"Generated: {now.strftime('%Y-%m-%d %H:%M UTC')} · window: last {window_days} days",
            "",
            "**Sources polled:**",
            "",
        ]
        # Sources header · names every source we tried + how many posts each
        # contributed within the window. Makes the digest self-describing.
        for source in store.list_sources():
            posts = store.items_for_source(source.id, cutoff_ts, item_type="post")
            url = source.url or ""
            link = f"[{source.name}]({url})" if url else source.name
            n = len(posts)
            lines.append(f"- {link} · `{source.type}` · {n} post{'s' if n != 1 else ''}")
        lines.append("")

        n_posts = 0
        for source in store.list_sources():
            posts = store.items_for_source(source.id, cutoff_ts, item_type="post")
            if not posts:
                continue
            lines.append(f"## {source.name}")
            lines.append("")
            for p, _ in posts:
                n_posts += 1
                pub = datetime.fromtimestamp(p.published_at, timezone.utc).strftime("%Y-%m-%d")
                permalink = p.metadata.get("permalink", "")
                score = p.metadata.get("score", 0)
                ncom = p.metadata.get("num_comments", 0)

                lines.append(f"### {p.title}")
                lines.append("")
                meta_bits = [f"Posted: {pub}", f"Author: u/{p.author}", f"Score: {score}", f"Comments: {ncom}"]
                lines.append(" · ".join(meta_bits))
                lines.append("")
                lines.append(f"Link: {p.url}")
                if permalink and p.url and not p.url.startswith("https://www.reddit.com"):
                    lines.append(f"Discussion: https://www.reddit.com{permalink}")
                lines.append("")

                if p.content.strip():
                    lines.append(p.content.strip())
                    lines.append("")

                comments = self._comments_for_post(store, source.id, p.id)
                if comments:
                    lines.append("**Top comments:**")
                    lines.append("")
                    for c in comments[:10]:
                        c_score = c.metadata.get("score", 0)
                        author = c.author or "[deleted]"
                        body = c.content.strip().replace("\n", " ")
                        if len(body) > 800:
                            body = body[:800] + "…"
                        lines.append(f"- *u/{author} (↑{c_score})*: {body}")
                    lines.append("")
                lines.append("---")
                lines.append("")

        Path(out_path).write_text("\n".join(lines), encoding="utf-8")
        return n_posts

    @staticmethod
    def _comments_for_post(store: Store, source_id: str, post_id: str) -> list[Item]:
        rows = store.conn.execute(
            """SELECT id, source_id, type, title, url, author, content, published_at, metadata
               FROM items
               WHERE source_id = ? AND type = 'comment'
                 AND json_extract(metadata, '$.parent_post_id') = ?
               ORDER BY json_extract(metadata, '$.score') DESC""",
            (source_id, post_id),
        ).fetchall()
        return [
            Item(
                id=r["id"], source_id=r["source_id"], type=r["type"],
                title=r["title"] or "", url=r["url"] or "",
                author=r["author"] or "", content=r["content"] or "",
                published_at=r["published_at"] or 0,
                metadata=json.loads(r["metadata"]) if r["metadata"] else {},
            )
            for r in rows
        ]


# --------- feed publisher (news-only) ---------

class FeedPublisher:
    """Generate today's feed by querying NotebookLM, publish to S3.

    Runs AFTER NotebookLMUploader.upload() · the notebook already has today's
    fresh digest indexed. We ask NotebookLM for the top-5 items (same prompt
    curator's laptop-side `feed` command used to use), parse the response,
    render the per-topic feed markdown, and put it to S3 so the laptop's
    curator can pull it without round-tripping through NotebookLM itself.
    """

    def __init__(self, notebook_id: str, topic: str, topic_name: str, bucket: str):
        self.notebook_id = notebook_id
        self.topic = topic
        self.topic_name = topic_name
        self.bucket = bucket

    def publish_for_today(self) -> str | None:
        from feed_prompt import FEED_PROMPT, parse_feed_response, render_item

        items = self._query_items(FEED_PROMPT, parse_feed_response)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        body = self._render_markdown(today, items, render_item)
        key = f"{self.topic}/{today}.md"

        try:
            import boto3
            s3 = boto3.client("s3")
            s3.put_object(
                Bucket=self.bucket, Key=key,
                Body=body.encode("utf-8"), ContentType="text/markdown",
            )
            logger.info(
                "feed: published %s (%d items, %d bytes) to s3://%s/%s",
                key, len(items), len(body), self.bucket, key,
            )
            return key
        except Exception as e:  # noqa: BLE001
            logger.warning("feed: S3 publish failed (non-fatal): %s", e)
            return None

    def _query_items(self, prompt: str, parser) -> list[dict]:
        import asyncio
        from notebooklm import NotebookLMClient

        async def _run() -> str:
            async with await NotebookLMClient.from_storage() as client:
                result = await client.chat.ask(self.notebook_id, prompt)
            answer = getattr(result, "answer", None)
            if not answer and isinstance(result, dict):
                answer = result.get("answer") or result.get("response") or ""
            return answer or str(result)

        try:
            answer = asyncio.run(_run())
        except Exception as e:  # noqa: BLE001
            logger.warning("feed: NotebookLM query failed (non-fatal): %s", e)
            return []
        return parser(answer)

    def _render_markdown(self, today: str, items: list[dict], render_item) -> str:
        notebooklm_url = NOTEBOOKLM_URL_TEMPLATE.format(notebook_id=self.notebook_id)
        lines = [
            "---",
            f"date: {today}",
            f"topics_included: [{self.topic}]",
            f"generated_at: {datetime.now(timezone.utc).isoformat()}",
            f"item_count: {len(items)}",
            "generated_at_commit: lambda",
            "---",
            "",
            f"# Newsfeed — {today}",
            "",
            f"## {self.topic}  ([{self.topic_name} notebook ↗]({notebooklm_url}))",
            "",
        ]
        if not items:
            lines.append("_(no notable items with citable URLs today)_")
        else:
            for item in items:
                lines.append(render_item(item))
        lines.append("")
        return "\n".join(lines)


# --------- post-upload hook · publishes NotebookLM-derived feed to S3 ---------

def _post_upload(cfg: TopicConfig, notebook_id: str | None, bucket: str | None, upload_ok: bool) -> None:
    if not bucket:
        return

    from curator.core.s3 import publish_to_s3
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # 1. The FeedPublisher derives a top-5 from NotebookLM and writes it as
    # the dated feed file. Only meaningful when the upload landed (otherwise
    # NotebookLM would return yesterday's items).
    if upload_ok and notebook_id:
        print(f"\nPublishing feed to s3://{bucket}/{FEED_TOPIC}/...")
        try:
            key = FeedPublisher(
                notebook_id=notebook_id, topic=FEED_TOPIC,
                topic_name=FEED_TOPIC_NAME, bucket=bucket,
            ).publish_for_today()
            print(f"  published: {key}" if key else "  publish: skipped (see logs)")
        except Exception as e:  # noqa: BLE001
            print(f"  publish failed: {e}")

    # 2. The HTML rendering of today's posts · published as the bookmarkable
    # daily-reader view. Independent of the NotebookLM upload outcome.
    html_key = f"{FEED_TOPIC}/{date_str}.html"
    try:
        publish_to_s3(cfg.html_path, bucket, html_key)
        print(f"  s3://{bucket}/{html_key}")
    except Exception as e:  # noqa: BLE001
        print(f"  HTML publish failed: {e}")
        logger.exception("s3 publish failed for %s", html_key)


# --------- topic config + entry point ---------

TOPIC = TopicConfig(
    name="scraping",
    db_path=DB_PATH,
    html_path=HTML_PATH,
    digest_path=DIGEST_PATH,
    sources=SOURCES,
    fetchers=FETCHERS,
    window_seconds=WINDOW_SECONDS,
    user_agent=USER_AGENT,
    notebook_env_var="NOTEBOOKLM_NOTEBOOK_ID",
    feed_bucket_env_var="FEED_BUCKET",
    s3_key_prefix=FEED_TOPIC,
    renderer_factory=Renderer,
    bundler_factory=Bundler,
    schema_migration=_migrate_legacy_reddit,
    post_upload_hook=_post_upload,
)


def main() -> None:
    run(TOPIC)


# Re-export NotebookLMUploader so legacy `from news_curator import …` chains
# can rebind to this module if anyone reaches for it.
__all__ = ["main", "TOPIC", "NotebookLMUploader"]


if __name__ == "__main__":
    main()
