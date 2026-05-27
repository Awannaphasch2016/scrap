"""rescore-llm Lambda · rescore jobs items via `claude -p` against the supply profile.

Triggered by Step Functions after the daily scrape Lambdas complete (planned;
see ~/.claude/plans/crispy-beaming-pike.md). Lifts items that still have
NULL relevance (just-ingested by the scrape Lambdas, which now write only to
metadata.regex_score), scores each via the LLM, and PATCHes the result into
curator.items.relevance + metadata.llm_reason via Supabase REST.

Cold-start order:
  1. _fetch_doppler_secrets()                   — pulls all scrape/dev secrets
  2. pull_credentials_to_home()                 — materializes CLAUDE_CREDENTIALS
                                                  to $HOME/.claude/.credentials.json
  3. load_profile()                             — S3 GetObject (cached in /tmp)
  4. fetch items (REST GET, relevance.is.null)
  5. ThreadPoolExecutor(5) → score + PATCH per item
  6. finally: storage.maybe_push_back()         — push rotated creds to Doppler
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

import requests

from curator.core.claude_credentials import pull_credentials_to_home
from curator.core.handler import _fetch_doppler_secrets
from curator.core.llm_scorer import LLMScorer
from curator.core.profile_loader import load_profile

logger = logging.getLogger()
logger.setLevel(logging.INFO)

WORKERS = 5
SCORE_WINDOW_HOURS = 36
DEFAULT_TOPIC = "jobs"


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
        "Prefer": "return=minimal",
    }


def _fetch_items(topic: str, force_all: bool) -> list[dict]:
    """GET items needing LLM scoring.

    Default: relevance IS NULL (items the scrape Lambda just ingested).
    force_all=True: every item in the window, regardless of relevance state
    (used to re-score after a profile change).
    """
    cutoff = (datetime.now(timezone.utc).timestamp()
              - SCORE_WINDOW_HOURS * 3600)
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
    params = {
        "topic": f"eq.{topic}",
        "published_at": f"gte.{cutoff_iso}",
        "select": "id,topic,title,author,content,published_at,metadata",
        "order": "published_at.desc",
        "limit": "500",
    }
    if not force_all:
        params["relevance"] = "is.null"
    r = requests.get(
        f"{_rest_base()}/rest/v1/items?{urllib.parse.urlencode(params)}",
        headers=_read_headers(), timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _patch_score(item: dict, llm: dict) -> None:
    """PATCH item with LLM score + reason. metadata.regex_score is preserved
    because we merge into the existing metadata dict."""
    new_meta = dict(item.get("metadata") or {})
    new_meta["llm_reason"] = llm["reason"]
    new_meta["llm_scored_at"] = datetime.now(timezone.utc).isoformat()
    body = {"relevance": llm["score"], "metadata": new_meta}
    params = {"id": f"eq.{item['id']}", "topic": f"eq.{item['topic']}"}
    r = requests.patch(
        f"{_rest_base()}/rest/v1/items?{urllib.parse.urlencode(params)}",
        headers=_write_headers(), data=json.dumps(body), timeout=30,
    )
    if r.status_code not in (200, 204):
        raise RuntimeError(f"PATCH failed HTTP {r.status_code}: {r.text[:200]}")


def _score_and_patch(scorer: LLMScorer, item: dict) -> tuple[dict, dict | None, str | None]:
    """Score one item + PATCH back. Returns (item, llm_result, err)."""
    # Adapt the REST row shape to the Item-like attribute access LLMScorer expects.
    class _ItemView:
        def __init__(self, d: dict) -> None:
            self.title = d.get("title")
            self.author = d.get("author")
            self.content = d.get("content")
            self.metadata = d.get("metadata") or {}

    try:
        llm = scorer.score(_ItemView(item))  # type: ignore[arg-type]
    except Exception as e:  # noqa: BLE001
        return (item, None, f"SCORE: {e}")
    try:
        _patch_score(item, llm)
    except Exception as e:  # noqa: BLE001
        return (item, llm, f"WRITE: {e}")
    return (item, llm, None)


def lambda_handler(event, context):  # noqa: ARG001
    """Entrypoint. Event payload (all optional):
        {
          "topic": "jobs",              # which topic to rescore
          "force_rescore_all": false,   # re-score even items that already have relevance
        }
    """
    _fetch_doppler_secrets()

    topic = (event or {}).get("topic", DEFAULT_TOPIC)
    force_all = bool((event or {}).get("force_rescore_all", False))

    summary: dict[str, Any] = {
        "topic": topic,
        "scored": 0, "failed": 0, "wall_s": 0.0,
        "credentials_rotated": False,
    }
    creds_storage = None

    try:
        # 1. Materialize claude OAuth credentials from Doppler
        creds_storage = pull_credentials_to_home()

        # 2. Load profile from S3 (cached in /tmp per cold-start)
        profile = load_profile()
        scorer = LLMScorer(profile=profile)

        # 3. Pull items to rescore
        items = _fetch_items(topic, force_all=force_all)
        logger.info("rescore-llm: %d items to score (topic=%s, force_all=%s)",
                    len(items), topic, force_all)
        summary["fetched"] = len(items)

        # 4. Score in parallel
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futures = {
                ex.submit(_score_and_patch, scorer, item): item
                for item in items
            }
            for fut in as_completed(futures):
                item, llm, err = fut.result()
                if err:
                    summary["failed"] += 1
                    logger.warning("rescore failed for %s: %s",
                                   item.get("id"), err)
                else:
                    summary["scored"] += 1
                    logger.info("rescored %s → %s",
                                item.get("id"), llm["score"])
        summary["wall_s"] = round(time.time() - t0, 1)

    finally:
        # 5. Push back rotated credentials regardless of run success
        if creds_storage is not None:
            try:
                rotated = creds_storage.maybe_push_back()
                summary["credentials_rotated"] = rotated
            except Exception as e:  # noqa: BLE001
                logger.warning("credentials push-back failed (non-fatal): %s", e)

    logger.info("rescore-llm summary: %s", summary)
    return {"status": "ok", **summary}
