"""S3-backed profile loader · fetches the supply paragraph for the LLM scorer.

The profile lives in S3 so it's editable iteratively without redeploying the
Lambda image. "Tune the scorer" = edit the markdown file, re-upload to S3,
next cold-start picks it up.

PROFILE_BUCKET env var holds the bucket name; PROFILE_KEY defaults to
'jobs_profile.md'. Cached in /tmp per Lambda container so warm invocations
skip the S3 round-trip (~50ms saved per invocation).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import boto3

logger = logging.getLogger(__name__)


DEFAULT_CACHE_PATH = Path("/tmp/curator_jobs_profile.md")


def load_profile(
    bucket: str | None = None,
    key: str | None = None,
    cache_path: Path = DEFAULT_CACHE_PATH,
    force_refresh: bool = False,
) -> str:
    """Return the profile paragraph as a stripped string.

    Reads from S3 once per Lambda container, caches in /tmp, returns the
    cached text on warm invocations. Falls back to env-var inputs if explicit
    bucket/key aren't passed.

    Raises RuntimeError if S3 fetch fails and no cache exists.
    """
    bucket = bucket or os.environ.get("PROFILE_BUCKET")
    key = key or os.environ.get("PROFILE_KEY", "jobs_profile.md")
    if not bucket:
        raise RuntimeError("PROFILE_BUCKET not set (env or argument)")

    if cache_path.exists() and not force_refresh:
        logger.info("profile: cache hit at %s", cache_path)
        return cache_path.read_text(encoding="utf-8").strip()

    s3 = boto3.client("s3")
    obj = s3.get_object(Bucket=bucket, Key=key)
    text = obj["Body"].read().decode("utf-8")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(text, encoding="utf-8")
    logger.info("profile: fetched s3://%s/%s (%d bytes) → %s",
                bucket, key, len(text), cache_path)
    return text.strip()
