"""TopicConfig · everything that varies between curator topics.

A topic module (curator/topics/<name>.py) constructs one `TopicConfig` and
hands it to `curator.core.pipeline.run`. The pipeline reads the config to
decide what to fetch, how to score it, where to persist, what to upload,
and where to publish — without knowing whether the topic is "scraping" or
"jobs" or a future addition.

Optional callables/factories let a topic plug in topic-specific behavior
without having to subclass the pipeline. Concrete examples:
  - news ships a FeedPublisher via `post_upload_hook` to query NotebookLM
    for top-5 and write the result to S3.
  - jobs ships a relevance scorer via `score_item` and writes per-day
    dated artifacts via its Bundler · the `post_upload_hook` uploads them.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from typing import Callable, Protocol

from curator.core.fetcher import Fetcher
from curator.core.types import Item


# A scorer takes an Item and returns (score, matched_keywords). Optional —
# topics without scoring leave TopicConfig.score_item as None.
Scorer = Callable[[Item], tuple[float, list[str]]]

# A schema migration runs once at Store __init__; receives the raw connection.
# Optional — only news inherited a legacy posts/comments → items migration.
SchemaMigration = Callable[[sqlite3.Connection], None]


class _RendererProtocol(Protocol):
    def render(self, store, out_path: str) -> None: ...


class _BundlerProtocol(Protocol):
    def export(self, store, out_path: str) -> int: ...


RendererFactory = Callable[["TopicConfig"], _RendererProtocol]
BundlerFactory = Callable[["TopicConfig"], _BundlerProtocol]

# Post-upload hook: runs after the NotebookLM upload attempt. Receives
# `(cfg, notebook_id_or_none, bucket_or_none, upload_ok)`. Topics use this for
# S3 publishing — news queries NotebookLM and writes a derived feed; jobs
# uploads the dated/top-N artifacts the Bundler just wrote to disk.
PostUploadHook = Callable[["TopicConfig", str | None, str | None, bool], None]


@dataclasses.dataclass(frozen=True)
class TopicConfig:
    # ----- identity -----
    name: str                   # "scraping" | "jobs" — also S3 prefix default

    # ----- persistence -----
    db_path: str
    html_path: str
    digest_path: str            # rotating canonical · uploaded to NotebookLM

    # ----- fetching -----
    sources: list[dict]
    fetchers: dict[str, Fetcher]
    window_seconds: float       # cutoff = now - window_seconds
    user_agent: str             # threaded into Fetcher subclasses via factory

    # ----- env-var hooks -----
    notebook_env_var: str       # "NOTEBOOKLM_NOTEBOOK_ID" | "JOBS_NOTEBOOK_ID"
    feed_bucket_env_var: str = "FEED_BUCKET"  # falls back to FEED_BUCKET if unset

    # ----- optional behaviors -----
    s3_key_prefix: str | None = None         # defaults to `name` at use site
    score_item: Scorer | None = None
    relevance_threshold: float = 0.0
    renderer_factory: RendererFactory | None = None
    bundler_factory: BundlerFactory | None = None
    schema_migration: SchemaMigration | None = None
    post_upload_hook: PostUploadHook | None = None

    @property
    def effective_s3_prefix(self) -> str:
        return self.s3_key_prefix or self.name
