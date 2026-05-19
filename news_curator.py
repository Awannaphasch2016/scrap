import dataclasses
import html
import json
import os
import sqlite3
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import requests

# --------- config ---------

DB_PATH = "news.db"
HTML_PATH = "news.html"
DIGEST_PATH = "digest.md"
WINDOW_DAYS = 30
USER_AGENT = "scrap-news-curator/0.2"
REQUEST_DELAY = 1.5

SOURCES = [
    {
        "type": "reddit",
        "id": "reddit:WebScraping",
        "name": "r/WebScraping",
        "url": "https://www.reddit.com/r/WebScraping/",
        "subreddit": "WebScraping",
    },
]


# --------- data model ---------

@dataclasses.dataclass
class Source:
    id: str
    name: str
    type: str
    url: str = ""


@dataclasses.dataclass
class Item:
    id: str
    source_id: str
    type: str  # "post" | "comment" | "article"
    title: str
    url: str
    author: str
    content: str
    published_at: float
    metadata: dict = dataclasses.field(default_factory=dict)


# --------- store ---------

class Store:
    def __init__(self, path: str = DB_PATH):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._setup()
        self._migrate_legacy()

    def _setup(self) -> None:
        # add 'type' column to legacy sources table if missing
        cur = self.conn.cursor()
        if cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sources'").fetchone():
            cols = {r[1] for r in cur.execute("PRAGMA table_info(sources)").fetchall()}
            if "type" not in cols:
                cur.execute("ALTER TABLE sources ADD COLUMN type TEXT NOT NULL DEFAULT 'reddit'")
                self.conn.commit()

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
                metadata TEXT,
                FOREIGN KEY (source_id) REFERENCES sources(id)
            );
            CREATE INDEX IF NOT EXISTS idx_items_source_published
                ON items(source_id, published_at DESC);
            CREATE INDEX IF NOT EXISTS idx_items_type ON items(type);
        """)
        self.conn.commit()

    def _migrate_legacy(self) -> None:
        cur = self.conn.cursor()
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
            # already migrated; drop legacy tables
            cur.executescript("DROP TABLE IF EXISTS comments; DROP TABLE IF EXISTS posts;")
            self.conn.commit()
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
        self.conn.commit()
        migrated = cur.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        print(f"  migrated to {migrated} items")

    def upsert_source(self, s: Source) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO sources (id, name, type, url) VALUES (?, ?, ?, ?)",
            (s.id, s.name, s.type, s.url),
        )
        self.conn.commit()

    def upsert_item(self, i: Item) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO items
               (id, source_id, type, title, url, author, content, published_at, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                i.id, i.source_id, i.type,
                i.title, i.url, i.author, i.content,
                i.published_at, json.dumps(i.metadata, ensure_ascii=False),
            ),
        )

    def commit(self) -> None:
        self.conn.commit()

    def list_sources(self) -> list[Source]:
        rows = self.conn.execute(
            "SELECT id, name, type, url FROM sources ORDER BY name"
        ).fetchall()
        return [Source(id=r["id"], name=r["name"], type=r["type"], url=r["url"] or "") for r in rows]

    def items_for_source(
        self, source_id: str, since_ts: float, item_type: str | None = None
    ) -> list[Item]:
        query = (
            "SELECT id, source_id, type, title, url, author, content, published_at, metadata "
            "FROM items WHERE source_id = ? AND published_at >= ?"
        )
        params: list = [source_id, since_ts]
        if item_type:
            query += " AND type = ?"
            params.append(item_type)
        query += " ORDER BY published_at DESC"
        rows = self.conn.execute(query, params).fetchall()
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


# --------- fetchers ---------

class Fetcher(ABC):
    @abstractmethod
    def fetch(self, source: Source, source_cfg: dict, cutoff_ts: float) -> Iterable[Item]:
        ...


class RedditFetcher(Fetcher):
    def fetch(self, source: Source, source_cfg: dict, cutoff_ts: float) -> Iterable[Item]:
        subreddit = source_cfg["subreddit"]
        for post in self._fetch_posts(subreddit, cutoff_ts):
            yield self._post_to_item(post, source.id)
            for c in self._fetch_top_level_comments(post["permalink"]):
                yield self._comment_to_item(c, source.id, post["id"])
            time.sleep(REQUEST_DELAY)

    def _fetch_posts(self, subreddit: str, cutoff_ts: float) -> list[dict]:
        posts: list[dict] = []
        after = None
        headers = {"User-Agent": USER_AGENT}
        while True:
            url = f"https://www.reddit.com/r/{subreddit}/new.json?limit=100"
            if after:
                url += f"&after={after}"
            http_proxy = os.environ.get("HTTP_PROXY")
            proxies = (
                {"http": http_proxy, "https": os.environ.get("HTTPS_PROXY", http_proxy)}
                if http_proxy else None
            )
            r = requests.get(url, headers=headers, timeout=15, proxies=proxies)
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
            time.sleep(REQUEST_DELAY)
        return posts

    def _fetch_top_level_comments(self, permalink: str) -> list[dict]:
        url = f"https://www.reddit.com{permalink}.json?limit=200&depth=1"
        headers = {"User-Agent": USER_AGENT}
        http_proxy = os.environ.get("HTTP_PROXY")
        proxies = (
            {"http": http_proxy, "https": os.environ.get("HTTPS_PROXY", http_proxy)}
            if http_proxy else None
        )
        try:
            r = requests.get(url, headers=headers, timeout=15, proxies=proxies)
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
        return Item(
            id=f"reddit:{c['id']}",
            source_id=source_id,
            type="comment",
            title="",
            url="",
            author=c.get("author", ""),
            content=html.unescape(c.get("body", "")),
            published_at=c.get("created_utc", 0),
            metadata={
                "score": c.get("score", 0),
                "parent_post_id": f"reddit:{parent_post_id}",
            },
        )


FETCHERS: dict[str, Fetcher] = {
    "reddit": RedditFetcher(),
}


# --------- renderer ---------

def _humanize_age(ts: float, now_ts: float) -> str:
    delta = now_ts - ts
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    return f"{int(delta / 86400)}d ago"


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
    def render(self, store: Store, out_path: str = HTML_PATH, window_days: int = WINDOW_DAYS) -> None:
        now = datetime.now(timezone.utc)
        cutoff_ts = (now - timedelta(days=window_days)).timestamp()

        sections: list[str] = []
        for source in store.list_sources():
            posts = store.items_for_source(source.id, cutoff_ts, item_type="post")
            items_html: list[str] = []
            for p in posts:
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
                window_days=window_days,
                sections="\n".join(sections),
            ),
            encoding="utf-8",
        )


# --------- bundler ---------

class Bundler:
    """Exports recent items to a single markdown file suitable for NotebookLM ingestion."""

    def export(
        self, store: Store, out_path: str = DIGEST_PATH, window_days: int = WINDOW_DAYS
    ) -> int:
        now = datetime.now(timezone.utc)
        cutoff_ts = (now - timedelta(days=window_days)).timestamp()

        lines: list[str] = [
            f"# Scraping News Digest",
            f"",
            f"Generated: {now.strftime('%Y-%m-%d %H:%M UTC')} · window: last {window_days} days",
            f"",
        ]
        n_posts = 0
        for source in store.list_sources():
            posts = store.items_for_source(source.id, cutoff_ts, item_type="post")
            if not posts:
                continue
            lines.append(f"## {source.name}")
            lines.append("")
            for p in posts:
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

    def _comments_for_post(self, store: Store, source_id: str, post_id: str) -> list[Item]:
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


# --------- uploader ---------

NOTEBOOKLM_STORAGE_PATH = Path.home() / ".notebooklm" / "storage_state.json"


def _ensure_notebooklm_storage() -> None:
    """Materialize NOTEBOOKLM_STORAGE_STATE (e.g. from Doppler) onto disk if missing.

    notebooklm-py expects a `storage_state.json` at ~/.notebooklm/. On portable runs
    (CI, new machine) the file won't exist locally — but the secret is in Doppler.
    Writes the env var to disk with 0o600 perms only when the file is absent or empty.
    """
    if NOTEBOOKLM_STORAGE_PATH.exists() and NOTEBOOKLM_STORAGE_PATH.stat().st_size > 0:
        return
    blob = os.environ.get("NOTEBOOKLM_STORAGE_STATE")
    if not blob:
        return
    NOTEBOOKLM_STORAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    NOTEBOOKLM_STORAGE_PATH.write_text(blob, encoding="utf-8")
    os.chmod(NOTEBOOKLM_STORAGE_PATH, 0o600)


class NotebookLMUploader:
    """Uploads a local file to a NotebookLM notebook. Lazy-imports notebooklm-py.

    Requires:
      - `pip install "notebooklm-py[browser]"`
      - Either a local `~/.notebooklm/storage_state.json` (from `notebooklm login`)
        or NOTEBOOKLM_STORAGE_STATE env var (e.g. injected via `doppler run`)
      - NOTEBOOKLM_NOTEBOOK_ID env var set to a notebook ID you've created
    """

    def __init__(self, notebook_id: str):
        self.notebook_id = notebook_id

    def upload(self, file_path: str) -> None:
        import asyncio

        _ensure_notebooklm_storage()
        from notebooklm import NotebookLMClient  # lazy import; optional dep

        target_title = Path(file_path).name

        async def _run() -> None:
            async with await NotebookLMClient.from_storage() as client:
                # rotate: delete any prior source with the same filename so the
                # notebook holds exactly one current digest, not one per day
                existing = await client.sources.list(self.notebook_id)
                for s in existing:
                    if getattr(s, "title", "") == target_title:
                        try:
                            await client.sources.delete(self.notebook_id, s.id)
                        except Exception as e:  # noqa: BLE001
                            print(f"  warning: could not delete prior {target_title}: {e}")
                await client.sources.add_file(self.notebook_id, file_path, wait=True)

        asyncio.run(_run())


# --------- pipeline ---------

def _source_from_cfg(cfg: dict) -> Source:
    return Source(id=cfg["id"], name=cfg["name"], type=cfg["type"], url=cfg.get("url", ""))


def main() -> None:
    print(f"Curating scraping news (last {WINDOW_DAYS} days)...")
    store = Store()

    cutoff_ts = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).timestamp()

    for cfg in SOURCES:
        source = _source_from_cfg(cfg)
        store.upsert_source(source)
        fetcher = FETCHERS.get(source.type)
        if not fetcher:
            print(f"[{source.name}] unknown source type: {source.type}; skipping")
            continue
        print(f"\n[{source.name}]")
        n_posts = n_comments = 0
        for item in fetcher.fetch(source, cfg, cutoff_ts):
            store.upsert_item(item)
            if item.type == "post":
                n_posts += 1
            elif item.type == "comment":
                n_comments += 1
            store.commit()
        print(f"  posts: {n_posts}, comments: {n_comments}")

    print("\nRendering HTML...")
    Renderer().render(store)
    print(f"  wrote {HTML_PATH}")

    print("\nBundling markdown digest...")
    n = Bundler().export(store)
    print(f"  wrote {DIGEST_PATH} ({n} posts)")

    notebook_id = os.environ.get("NOTEBOOKLM_NOTEBOOK_ID")
    if notebook_id:
        print(f"\nUploading to NotebookLM (notebook {notebook_id})...")
        try:
            NotebookLMUploader(notebook_id).upload(DIGEST_PATH)
            print("  upload complete")
        except ImportError:
            print("  notebooklm-py not installed; run: pip install 'notebooklm-py[browser]'")
        except Exception as e:
            print(f"  upload failed: {e}")
    else:
        print("\n(set NOTEBOOKLM_NOTEBOOK_ID to auto-upload digest.md after each run)")


if __name__ == "__main__":
    main()
