"""Supabase mirror of the local SQLite Store · canonical state for the /curator UI.

The Lambda's working memory during a run is still SQLite in /tmp (cheap,
in-process, transactional). At the END of the run we bulk-flush sources +
items into Supabase Postgres so the personal-website /curator page can
query and Realtime-subscribe to canonical state.

Why direct Postgres (psycopg2) instead of Supabase REST/PostgREST:
  - Lambda only writes, never reads · REST adds an HTTP per row vs one TCP
    connection with execute_values for ~40 rows
  - No secret-key (sb_secret_xxx / legacy service_role) needed · direct PG
    auth via SUPABASE_DATABASE_URL · the URL's embedded password authenticates
    the Lambda as the database superuser, which RLS doesn't gate
  - One-shot connection per invocation · no pool maintenance
  - psycopg2.extras.execute_values batches the insert into one round-trip

Failure semantics:
  - Non-fatal · if Supabase is unreachable, the run still publishes to S3
    archive + NotebookLM. The next day's run will re-flush whatever didn't
    make it (ON CONFLICT DO UPDATE makes the upsert idempotent).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Iterable

import psycopg2
import psycopg2.extras
import requests
from psycopg2.extras import Json  # adapter · dict → jsonb without intermediate stringification

from curator.core.store import Store as SqliteStore
from curator.core.types import Item, Source

logger = logging.getLogger(__name__)


_UPSERT_SOURCES = """
insert into curator.sources (topic, id, name, type, url)
values %s
on conflict (topic, id) do update set
    name = excluded.name,
    type = excluded.type,
    url  = excluded.url
"""

_UPSERT_ITEMS = """
insert into curator.items (
    topic, id, source_id, type, title, url, author, content,
    published_at, fetched_at, relevance, metadata
)
values %s
on conflict (topic, id) do update set
    source_id    = excluded.source_id,
    type         = excluded.type,
    title        = excluded.title,
    url          = excluded.url,
    author       = excluded.author,
    content      = excluded.content,
    published_at = excluded.published_at,
    fetched_at   = excluded.fetched_at,
    relevance    = excluded.relevance,
    metadata     = excluded.metadata
"""


def _ts_to_dt(ts: float) -> datetime | None:
    """Convert curator's unix-epoch float timestamps to timestamptz-friendly
    datetimes. Zero / None → None so we don't store epoch=0 rows."""
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)


class SupabaseStore:
    """Thin upserter against the curator.{sources,items} tables.

    Use as a context manager so the connection is closed deterministically.
    """

    def __init__(self, dsn: str, *, connect_timeout_s: int = 15) -> None:
        self._dsn = dsn
        self._connect_timeout_s = connect_timeout_s
        self._conn: psycopg2.extensions.connection | None = None

    def __enter__(self) -> "SupabaseStore":
        self._conn = psycopg2.connect(self._dsn, connect_timeout=self._connect_timeout_s)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None

    # ----- bulk upsert -----

    def upsert_sources(self, topic: str, sources: Iterable[Source]) -> int:
        rows = [(topic, s.id, s.name, s.type, s.url) for s in sources]
        if not rows:
            return 0
        with self._conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, _UPSERT_SOURCES, rows, page_size=200)
        self._conn.commit()
        return len(rows)

    def upsert_items(
        self,
        topic: str,
        items_with_score: Iterable[tuple[Item, float | None]],
    ) -> int:
        now = datetime.now(timezone.utc)
        rows = []
        for item, relevance in items_with_score:
            rows.append((
                topic,
                item.id,
                item.source_id,
                item.type,
                item.title or None,
                item.url or None,
                item.author or None,
                item.content or None,
                _ts_to_dt(item.published_at),
                now,
                relevance,
                Json(item.metadata or {}),
            ))
        if not rows:
            return 0
        with self._conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, _UPSERT_ITEMS, rows, page_size=200)
        self._conn.commit()
        return len(rows)

    # ----- convenience: flush a local Store wholesale -----

    def flush_from(self, store: SqliteStore, topic: str) -> tuple[int, int]:
        """Copy every source + item from the local SQLite Store to Supabase
        under the given topic name. Idempotent · safe to re-run.

        Returns (n_sources, n_items).
        """
        sources = store.list_sources()
        n_sources = self.upsert_sources(topic, sources)

        items_with_score = [
            (item, score)
            for (item, score, _source_name) in store.all_items(min_relevance=-1e9)
        ]
        n_items = self.upsert_items(topic, items_with_score)
        return n_sources, n_items


# ===== REST writer (Door A) =====
#
# Writes via PostgREST instead of the Postgres wire protocol. Bypasses the
# project's Network Restrictions allow-list (which only fences the Postgres
# protocol). Auth = secret key as service_role. Requires INSERT/UPDATE/DELETE
# grants on the target tables — granted via Management API.

class RestWriter:
    """Same upsert surface as SupabaseStore, but over PostgREST.

    Use as a context manager · the session is reused across upsert calls.
    """

    def __init__(
        self,
        base_url: str,
        secret_key: str,
        *,
        schema: str = "curator",
        timeout_s: int = 30,
        batch_size: int = 200,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._secret = secret_key
        self._schema = schema
        self._timeout = timeout_s
        self._batch_size = batch_size
        self._session: requests.Session | None = None

    def __enter__(self) -> "RestWriter":
        s = requests.Session()
        s.headers.update({
            "apikey": self._secret,
            "Authorization": f"Bearer {self._secret}",
            "Content-Profile": self._schema,
            "Content-Type": "application/json",
            # merge-duplicates = UPSERT; return=minimal saves bandwidth
            "Prefer": "resolution=merge-duplicates,return=minimal",
        })
        self._session = s
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except Exception:  # noqa: BLE001
                pass
            self._session = None

    def _upsert(self, table: str, on_conflict: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        url = f"{self._base_url}/rest/v1/{table}?on_conflict={on_conflict}"
        for i in range(0, len(rows), self._batch_size):
            chunk = rows[i:i + self._batch_size]
            r = self._session.post(url, json=chunk, timeout=self._timeout)
            if r.status_code not in (200, 201, 204):
                raise RuntimeError(
                    f"REST upsert {table} (batch {i}:{i+len(chunk)}) failed: "
                    f"HTTP {r.status_code} · {r.text[:300]}"
                )
        return len(rows)

    # ----- bulk upsert -----

    def upsert_sources(self, topic: str, sources: Iterable[Source]) -> int:
        rows = [
            {"topic": topic, "id": s.id, "name": s.name, "type": s.type, "url": s.url or None}
            for s in sources
        ]
        return self._upsert("sources", "topic,id", rows)

    def upsert_items(
        self,
        topic: str,
        items_with_score: Iterable[tuple[Item, float | None]],
    ) -> int:
        now_iso = datetime.now(timezone.utc).isoformat()
        rows = []
        for item, relevance in items_with_score:
            pub_dt = _ts_to_dt(item.published_at)
            rows.append({
                "topic": topic,
                "id": item.id,
                "source_id": item.source_id,
                "type": item.type,
                "title": item.title or None,
                "url": item.url or None,
                "author": item.author or None,
                "content": item.content or None,
                "published_at": pub_dt.isoformat() if pub_dt else None,
                "fetched_at": now_iso,
                "relevance": relevance,
                "metadata": item.metadata or {},
            })
        return self._upsert("items", "topic,id", rows)

    # ----- convenience: flush a local Store wholesale -----

    def flush_from(self, store: SqliteStore, topic: str) -> tuple[int, int]:
        sources = store.list_sources()
        n_sources = self.upsert_sources(topic, sources)
        items_with_score = [
            (item, score)
            for (item, score, _source_name) in store.all_items(min_relevance=-1e9)
        ]
        n_items = self.upsert_items(topic, items_with_score)
        return n_sources, n_items


# ===== Writer-backend selection =====

def get_writer_or_none() -> "SupabaseStore | RestWriter | None":
    """Return a writer for the configured Supabase backend, or None if not configured.

    Backend selection:
      SUPABASE_BACKEND = "direct" | "pooler" | "rest"  (default: "pooler")
      SUPABASE_DSN_DIRECT  = DSN for db.<ref>.supabase.co:5432 (used when direct)
      SUPABASE_DSN_POOLER  = DSN for aws-1-<region>.pooler.supabase.com:6543 (used when pooler)

    Back-compat: if no backend env vars are set but the legacy
    SUPABASE_DATABASE_URL is, treat it as a direct DSN.

    The "rest" backend isn't implemented yet — raises NotImplementedError if
    selected, so a misconfigured run fails loudly instead of silently dropping
    items on the floor.
    """
    backend = os.environ.get("SUPABASE_BACKEND", "").lower()
    legacy = os.environ.get("SUPABASE_DATABASE_URL")

    # Nothing configured at all
    if not backend and not legacy:
        return None

    # Legacy fallback when no explicit backend is set
    if not backend:
        return SupabaseStore(legacy)

    if backend == "direct":
        dsn = os.environ.get("SUPABASE_DSN_DIRECT") or legacy
        if not dsn:
            raise RuntimeError("SUPABASE_BACKEND=direct but neither SUPABASE_DSN_DIRECT nor SUPABASE_DATABASE_URL is set")
        return SupabaseStore(dsn)

    if backend == "pooler":
        dsn = os.environ.get("SUPABASE_DSN_POOLER")
        if not dsn:
            raise RuntimeError("SUPABASE_BACKEND=pooler but SUPABASE_DSN_POOLER is not set")
        return SupabaseStore(dsn)

    if backend == "rest":
        url = os.environ.get("PUBLIC_SUPABASE_URL")
        key = os.environ.get("SUPABASE_SECRET_KEY")
        if not url or not key:
            raise RuntimeError(
                "SUPABASE_BACKEND=rest requires PUBLIC_SUPABASE_URL and SUPABASE_SECRET_KEY"
            )
        return RestWriter(url, key)

    raise RuntimeError(f"unknown SUPABASE_BACKEND: {backend!r} (expected: direct | pooler | rest)")
