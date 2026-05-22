"""Topic-agnostic pipeline · the orchestrator that `run(cfg)` drives.

Steps, in order:
  1. Open the Store (idempotent schema setup + topic's legacy migration).
  2. For each source in cfg.sources, dispatch to cfg.fetchers[source.type]
     and upsert items (with optional relevance score).
  3. Render HTML via cfg.renderer_factory(cfg).
  4. Bundle markdown digest via cfg.bundler_factory(cfg).
  5. Upload the canonical digest to NotebookLM (rotating source) if
     <cfg.notebook_env_var> is set.
  6. Invoke cfg.post_upload_hook(cfg, notebook_id, bucket, upload_ok)
     if both the hook and a bucket are configured — topics use this for
     S3 publishing in whatever shape they need.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from curator.core.notebooklm import NotebookLMUploader
from curator.core.store import Store
from curator.core.topic_config import TopicConfig
from curator.core.types import Source

logger = logging.getLogger(__name__)


def run(cfg: TopicConfig) -> None:
    threshold_note = (
        f", threshold ≥ {cfg.relevance_threshold:g}" if cfg.score_item else ""
    )
    print(f"Curating {cfg.name} (window: {cfg.window_seconds / 3600:g}h{threshold_note})...")

    store = Store(cfg.db_path, legacy_migration=cfg.schema_migration)
    cutoff_ts = (datetime.now(timezone.utc) - timedelta(seconds=cfg.window_seconds)).timestamp()

    total_fetched = total_kept = 0
    for src_cfg in cfg.sources:
        source = Source(
            id=src_cfg["id"],
            name=src_cfg["name"],
            type=src_cfg["type"],
            url=src_cfg.get("url", ""),
        )
        store.upsert_source(source)
        fetcher = cfg.fetchers.get(source.type)
        if not fetcher:
            print(f"[{source.name}] unknown source type: {source.type}; skipping")
            continue
        print(f"\n[{source.name}]")
        n_fetched = n_kept = 0
        try:
            for item in fetcher.fetch(source, src_cfg, cutoff_ts):
                n_fetched += 1
                score = None
                if cfg.score_item:
                    score, _ = cfg.score_item(item)
                store.upsert_item(item, score)
                if score is None or score >= cfg.relevance_threshold:
                    n_kept += 1
            store.commit()
        except Exception as e:  # noqa: BLE001
            print(f"  fetcher error: {e}")
            logger.exception("fetcher %s failed", source.id)
            continue
        suffix = (
            f" (score ≥ {cfg.relevance_threshold:g})" if cfg.score_item else ""
        )
        print(f"  fetched: {n_fetched}, kept{suffix}: {n_kept}")
        total_fetched += n_fetched
        total_kept += n_kept

    print(f"\nTotals: {total_fetched} fetched, {total_kept} kept")

    if cfg.renderer_factory:
        print("\nRendering HTML...")
        cfg.renderer_factory(cfg).render(store, cfg.html_path)
        print(f"  wrote {cfg.html_path}")

    if cfg.bundler_factory:
        print("\nBundling markdown digest...")
        n = cfg.bundler_factory(cfg).export(store, cfg.digest_path)
        print(f"  wrote {cfg.digest_path} ({n} items)")

    notebook_id = os.environ.get(cfg.notebook_env_var)
    upload_ok = False
    if notebook_id:
        print(f"\nUploading to NotebookLM (notebook {notebook_id})...")
        try:
            NotebookLMUploader(notebook_id).upload(cfg.digest_path)
            print("  upload complete")
            upload_ok = True
        except ImportError:
            print("  notebooklm-py not installed; pip install 'notebooklm-py[browser]'")
        except Exception as e:  # noqa: BLE001
            print(f"  upload failed: {e}")
            logger.exception("notebooklm upload failed")
    else:
        print(f"\n(set {cfg.notebook_env_var} to auto-upload digest to NotebookLM)")

    bucket = os.environ.get(cfg.feed_bucket_env_var) or os.environ.get("FEED_BUCKET")
    if cfg.post_upload_hook:
        if bucket:
            cfg.post_upload_hook(cfg, notebook_id, bucket, upload_ok)
        else:
            print(f"\n(set {cfg.feed_bucket_env_var} or FEED_BUCKET to publish to S3)")
