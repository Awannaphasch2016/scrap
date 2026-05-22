"""SQLite persistence for topic curators · unified schema across topics.

One `sources` table + one `items` table, schema designed so any topic can use
it. The `items.relevance` column is REAL and nullable — topics with a relevance
scorer (jobs) write a score; topics without one (news) leave it NULL.

External callers always get `list[tuple[Item, float | None]]` from queries so
the call shape is uniform — destructure with `for item, score in rows:` and
ignore `score` if you're a topic that doesn't score.

Optional `legacy_migration` callback at construction is for topics that own a
one-time schema migration from an earlier shape (news inherited the original
posts/comments split; jobs has no legacy). The callback receives the raw
sqlite3.Connection so it can do whatever it needs.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Callable

from curator.core.types import Item, Source


class Store:
    def __init__(
        self,
        path: str,
        *,
        legacy_migration: Callable[[sqlite3.Connection], None] | None = None,
    ) -> None:
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._setup()
        if legacy_migration:
            legacy_migration(self.conn)
            self.conn.commit()

    # ----- schema -----

    def _setup(self) -> None:
        cur = self.conn.cursor()

        # add 'type' column to legacy `sources` table if missing (inherited
        # from news_curator's original schema; harmless on fresh DBs).
        if cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sources'"
        ).fetchone():
            cols = {r[1] for r in cur.execute("PRAGMA table_info(sources)").fetchall()}
            if "type" not in cols:
                cur.execute(
                    "ALTER TABLE sources ADD COLUMN type TEXT NOT NULL DEFAULT 'reddit'"
                )
                self.conn.commit()

        # create tables · IF NOT EXISTS is a no-op on existing DBs that may
        # be missing the relevance column (handled by the ALTER below).
        self.conn.executescript(
            """
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
                relevance REAL,
                metadata TEXT,
                FOREIGN KEY (source_id) REFERENCES sources(id)
            );
            """
        )

        # Idempotent ALTER for production news.db, which predates the
        # relevance column. PRAGMA-guarded so it's a no-op on DBs that
        # already have it. Must run BEFORE creating the relevance index.
        cols = {r[1] for r in cur.execute("PRAGMA table_info(items)").fetchall()}
        if "relevance" not in cols:
            cur.execute("ALTER TABLE items ADD COLUMN relevance REAL")

        # create indexes · now safe to reference `relevance` on either DB.
        self.conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_items_source_published
                ON items(source_id, published_at DESC);
            CREATE INDEX IF NOT EXISTS idx_items_type ON items(type);
            CREATE INDEX IF NOT EXISTS idx_items_relevance
                ON items(relevance DESC, published_at DESC);
            """
        )

        self.conn.commit()

    # ----- writers -----

    def upsert_source(self, s: Source) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO sources (id, name, type, url) VALUES (?, ?, ?, ?)",
            (s.id, s.name, s.type, s.url),
        )
        self.conn.commit()

    def upsert_item(self, i: Item, relevance: float | None = None) -> None:
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

    # ----- readers -----

    def list_sources(self) -> list[Source]:
        rows = self.conn.execute(
            "SELECT id, name, type, url FROM sources ORDER BY name"
        ).fetchall()
        return [
            Source(id=r["id"], name=r["name"], type=r["type"], url=r["url"] or "")
            for r in rows
        ]

    def items_for_source(
        self,
        source_id: str,
        since_ts: float,
        *,
        item_type: str | None = None,
        min_relevance: float = 0.0,
    ) -> list[tuple[Item, float | None]]:
        """Return (item, relevance) pairs for one source within the time window.

        `item_type` filter (news: post/comment) and `min_relevance` filter (jobs)
        compose; topics that don't care about a filter pass the default.
        Ordering: relevance DESC NULLS LAST, then published_at DESC — so
        scored topics get score-first ordering, unscored topics get
        recency-first (NULLs sort last in SQLite when DESC).
        """
        query = (
            "SELECT id, source_id, type, title, url, author, content, "
            "published_at, relevance, metadata "
            "FROM items WHERE source_id = ? AND published_at >= ? "
            "AND (relevance IS NULL OR relevance >= ?)"
        )
        params: list = [source_id, since_ts, min_relevance]
        if item_type:
            query += " AND type = ?"
            params.append(item_type)
        query += " ORDER BY relevance DESC, published_at DESC"
        rows = self.conn.execute(query, params).fetchall()
        return [self._row_to_pair(r) for r in rows]

    def all_items(
        self,
        *,
        min_relevance: float = 0.0,
        since_ts: float | None = None,
    ) -> list[tuple[Item, float | None, str]]:
        """(item, relevance, source_name) across all sources, sorted score → recency."""
        params: list = [min_relevance]
        where = "(i.relevance IS NULL OR i.relevance >= ?)"
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
        out: list[tuple[Item, float | None, str]] = []
        for r in rows:
            item, score = self._row_to_pair(r)
            out.append((item, score, r["source_name"]))
        return out

    @staticmethod
    def _row_to_pair(r: sqlite3.Row) -> tuple[Item, float | None]:
        item = Item(
            id=r["id"], source_id=r["source_id"], type=r["type"],
            title=r["title"] or "", url=r["url"] or "",
            author=r["author"] or "", content=r["content"] or "",
            published_at=r["published_at"] or 0,
            metadata=json.loads(r["metadata"]) if r["metadata"] else {},
        )
        relevance = r["relevance"] if "relevance" in r.keys() else None
        return item, relevance
