"""generate-summary Lambda · ephemeral NotebookLM notebook + text/audio fan-out.

Triggered by Step Functions in parallel with rescore-llm after the daily scrape
branch completes. One execution does the full lifecycle:

  1. Pull NotebookLM cookies (DopplerStorage)
  2. Query Supabase for items fetched in the last ~6h (the SFN run window)
  3. Render those items into one markdown blob (curator.core.summary_renderer)
  4. NotebooksAPI.create(title) — fresh ephemeral notebook per day
  5. SourcesAPI.add_text(notebook, blob, wait=True)
  6. asyncio.gather(
        ArtifactsAPI.generate_report → wait_for_completion → download_report,
        ArtifactsAPI.generate_audio  → wait_for_completion → download_audio,
     )
  7. UPSERT curator.daily_summaries row (text in DB; audio s3 key reference)
  8. Upload audio to s3://${FEED_BUCKET}/summary/YYYY-MM-DD.mp4
  9. NotebooksAPI.delete(notebook) — always, even on failure
  10. DopplerStorage.maybe_push_back() for rotated cookies
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.parse
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import boto3
import requests

from curator.core.handler import _fetch_doppler_secrets, _prepare_writable_layout
from curator.core.notebooklm import (
    _doppler_storage_or_none,
    _ensure_notebooklm_storage,
)
from curator.core.summary_renderer import render_summary_source_md

logger = logging.getLogger()
logger.setLevel(logging.INFO)

WINDOW_HOURS = 6           # SFN run window — covers a daily scrape + buffer
TMP_AUDIO_PATH = Path("/tmp/summary_audio.mp4")
TMP_TEXT_PATH = Path("/tmp/summary_text.md")


# ---- Supabase REST helpers ----

def _rest_base() -> str:
    return os.environ["PUBLIC_SUPABASE_URL"].rstrip("/")


def _read_headers() -> dict:
    pub = os.environ["PUBLIC_SUPABASE_PUBLISHABLE_KEY"]
    return {
        "apikey": pub,
        "Authorization": f"Bearer {pub}",
        "Accept-Profile": "curator",
        "Accept": "application/json",
    }


def _write_headers() -> dict:
    sec = os.environ["SUPABASE_SECRET_KEY"]
    return {
        "apikey": sec,
        "Authorization": f"Bearer {sec}",
        "Content-Profile": "curator",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }


def _fetch_recent_items(window_hours: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc).timestamp() - window_hours * 3600
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
    params = {
        "fetched_at": f"gte.{cutoff_iso}",
        "select": "id,topic,source_id,title,url,author,content,published_at,relevance,metadata",
        "order": "topic.asc,relevance.desc.nullslast",
        "limit": "500",
    }
    r = requests.get(
        f"{_rest_base()}/rest/v1/items?{urllib.parse.urlencode(params)}",
        headers=_read_headers(), timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _upsert_summary_row(
    summary_date: date, text_md: str, audio_s3_key: str | None,
    notebook_id: str, item_count: int,
) -> None:
    """UPSERT on date PK; re-runs replace the row."""
    body = [{
        "date": summary_date.isoformat(),
        "text_md": text_md,
        "audio_s3_key": audio_s3_key,
        "notebook_id": notebook_id,
        "item_count": item_count,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }]
    r = requests.post(
        f"{_rest_base()}/rest/v1/daily_summaries?on_conflict=date",
        headers=_write_headers(), data=json.dumps(body), timeout=30,
    )
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"daily_summaries UPSERT failed HTTP {r.status_code}: {r.text[:200]}")


# ---- NotebookLM cookie auth (reuses notebooklm.py helpers) ----

def _materialize_cookies():
    storage = _doppler_storage_or_none()
    if storage is not None:
        try:
            storage.pull_to_local()
        except Exception as e:  # noqa: BLE001
            logger.warning("doppler pull failed (%s); falling back to env blob", e)
            _ensure_notebooklm_storage()
    else:
        _ensure_notebooklm_storage()
    return storage


# ---- The actual async pipeline ----

async def _generate_text_and_audio(client, notebook_id: str) -> tuple[str, str]:
    """Fan out: generate notes + audio in parallel, return (text, audio_path)."""

    async def _do_text() -> str:
        status = await client.artifacts.generate_report(notebook_id)
        await client.artifacts.wait_for_completion(notebook_id, status.task_id)
        await client.artifacts.download_report(notebook_id, str(TMP_TEXT_PATH))
        return TMP_TEXT_PATH.read_text(encoding="utf-8")

    async def _do_audio() -> str:
        status = await client.artifacts.generate_audio(notebook_id)
        await client.artifacts.wait_for_completion(notebook_id, status.task_id)
        await client.artifacts.download_audio(notebook_id, str(TMP_AUDIO_PATH))
        return str(TMP_AUDIO_PATH)

    text, audio_path = await asyncio.gather(_do_text(), _do_audio())
    return text, audio_path


async def _run(event: dict[str, Any]) -> dict[str, Any]:
    today = date.today()
    summary: dict[str, Any] = {
        "status": "ok",
        "date": today.isoformat(),
        "notebook_id": None,
        "item_count": 0,
        "text_chars": 0,
        "audio_s3_key": None,
        "credentials_rotated": False,
    }

    # 1. Cookies
    cookie_storage = _materialize_cookies()

    # 2. Items
    items = _fetch_recent_items(window_hours=WINDOW_HOURS)
    summary["item_count"] = len(items)
    if not items:
        logger.info("no items in window; skipping summary generation")
        summary["status"] = "skipped"
        return summary

    # 3. Render markdown source
    source_md = render_summary_source_md(items)
    logger.info("rendered source markdown · %d chars · %d items", len(source_md), len(items))

    # 4-9. Create → upload → fan-out → download → persist → delete
    from notebooklm import NotebookLMClient  # lazy import (heavy)

    notebook_id: str | None = None
    async with await NotebookLMClient.from_storage() as client:
        try:
            title = f"curator-daily-{today.isoformat()}"
            nb = await client.notebooks.create(title=title)
            notebook_id = nb.id
            summary["notebook_id"] = notebook_id
            logger.info("created ephemeral notebook %s (%s)", notebook_id, title)

            await client.sources.add_text(
                notebook_id, content=source_md,
                title=f"{title}.md", wait=True,
            )
            logger.info("uploaded source · waiting for processing complete")

            text_md, audio_path = await _generate_text_and_audio(client, notebook_id)
            summary["text_chars"] = len(text_md)
            logger.info("downloaded text (%d chars) + audio (%s)",
                        len(text_md), audio_path)

            # 7-8. Persist
            audio_s3_key = f"summary/{today.isoformat()}.mp4"
            bucket = os.environ["FEED_BUCKET"]
            boto3.client("s3").upload_file(
                audio_path, bucket, audio_s3_key,
                ExtraArgs={"ContentType": "audio/mp4"},
            )
            summary["audio_s3_key"] = audio_s3_key
            logger.info("uploaded audio → s3://%s/%s", bucket, audio_s3_key)

            _upsert_summary_row(
                today, text_md, audio_s3_key, notebook_id, len(items),
            )
            logger.info("upserted curator.daily_summaries row")

        finally:
            # 9. Delete the ephemeral notebook regardless of outcome
            if notebook_id is not None:
                try:
                    await client.notebooks.delete(notebook_id)
                    logger.info("deleted ephemeral notebook %s", notebook_id)
                except Exception as e:  # noqa: BLE001
                    logger.warning("delete failed (non-fatal): %s", e)

    # 10. Cookie push-back
    if cookie_storage is not None:
        try:
            summary["credentials_rotated"] = cookie_storage.maybe_push_back()
        except Exception as e:  # noqa: BLE001
            logger.warning("cookie push-back failed (non-fatal): %s", e)

    return summary


def lambda_handler(event, context):  # noqa: ARG001
    _prepare_writable_layout()      # HOME=/tmp · /var/task is read-only
    _fetch_doppler_secrets()
    result = asyncio.run(_run(event or {}))
    logger.info("generate-summary result: %s",
                {k: v for k, v in result.items() if k != "text_md"})
    return result
