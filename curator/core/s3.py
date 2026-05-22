"""S3 publish helper · shared by every topic that writes per-day artifacts.

Lazy-imports boto3 so unit tests + local invocations without an S3 bucket
don't pay the import cost or need AWS credentials. Lambda already has boto3
preinstalled in the runtime image; local dev installs it on demand.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


_CONTENT_TYPE_BY_EXT = {
    ".md":   "text/markdown; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".htm":  "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".txt":  "text/plain; charset=utf-8",
}


def publish_to_s3(
    local_path: str, bucket: str, key: str, content_type: str | None = None
) -> None:
    """Upload a local file to s3://<bucket>/<key>.

    Content-Type defaults to a sensible guess from the key extension
    (.md → text/markdown, .html → text/html, …). Falls back to
    application/octet-stream for unknowns. Pass `content_type` to override.

    Caller is responsible for failure handling · in the curator pipeline a
    publish failure is logged at warning level (the bundler already wrote
    the local file, so the day's artifact survives even if S3 is unreachable).
    """
    import boto3  # noqa: PLC0415 — runtime import keeps non-AWS uses cheap

    if content_type is None:
        ext = Path(key).suffix.lower()
        content_type = _CONTENT_TYPE_BY_EXT.get(ext, "application/octet-stream")

    s3 = boto3.client("s3")
    body = Path(local_path).read_bytes()
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)
    logger.info(
        "s3: published %s → s3://%s/%s (%d bytes, %s)",
        local_path, bucket, key, len(body), content_type,
    )
