"""S3 publish helper · shared by every topic that writes per-day artifacts.

Lazy-imports boto3 so unit tests + local invocations without an S3 bucket
don't pay the import cost or need AWS credentials. Lambda already has boto3
preinstalled in the runtime image; local dev installs it on demand.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def publish_to_s3(local_path: str, bucket: str, key: str) -> None:
    """Upload a local file to s3://<bucket>/<key> with text/markdown content-type.

    Caller is responsible for failure handling · in the curator pipeline a
    publish failure is logged at warning level (the bundler already wrote
    the local file, so the day's artifact survives even if S3 is unreachable).
    """
    import boto3  # noqa: PLC0415 — runtime import keeps non-AWS uses cheap

    s3 = boto3.client("s3")
    body = Path(local_path).read_bytes()
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="text/markdown")
    logger.info(
        "s3: published %s → s3://%s/%s (%d bytes)",
        local_path, bucket, key, len(body),
    )
