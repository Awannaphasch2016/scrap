"""Doppler-backed `claude` CLI credential rotation · reuses DopplerStorage.

The `claude` CLI reads OAuth credentials from $HOME/.claude/.credentials.json
and rotates the access token by writing back to the same file. In Lambda
that file is ephemeral (writable /tmp, but lost between cold-starts and
unrelated invocations), so we use the same pull-on-cold-start / push-back-
on-rotation pattern that NotebookLM uses for its cookie blob.

Doppler secret: CLAUDE_CREDENTIALS (the verbatim contents of the laptop's
~/.claude/.credentials.json, seeded once by the developer).

Lambda contract:
  - ENV HOME=/tmp/home in Dockerfile.rescore (writable, rotation-friendly)
  - At handler start: call pull_credentials_to_home() — fetches the secret,
    writes to $HOME/.claude/.credentials.json, returns a DopplerStorage
    instance the caller can pass to maybe_push_back_credentials() in a
    finally block.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from curator.core.notebooklm import DopplerStorage

logger = logging.getLogger(__name__)

CLAUDE_CREDENTIALS_SECRET = "CLAUDE_CREDENTIALS"


def credentials_path() -> Path:
    """Where the `claude` CLI looks for credentials, given the current HOME."""
    return Path(os.environ.get("HOME", str(Path.home()))) / ".claude" / ".credentials.json"


def pull_credentials_to_home(
    project: str = "scrape",
    config: str = "dev",
    token: str | None = None,
) -> DopplerStorage:
    """Fetch CLAUDE_CREDENTIALS from Doppler, materialize to $HOME/.claude/.

    Returns the DopplerStorage instance so the caller can later call
    .maybe_push_back() in a finally block, capturing any rotated tokens.

    Token defaults to DOPPLER_WRITE_TOKEN if set, else DOPPLER_TOKEN —
    same precedence as NotebookLM uses. Push-back requires write scope; on a
    read-only token push() will 403 and DopplerStorage logs a warning
    (never raises).
    """
    token = token or os.environ.get("DOPPLER_WRITE_TOKEN") or os.environ.get("DOPPLER_TOKEN")
    if not token:
        raise RuntimeError(
            "neither DOPPLER_WRITE_TOKEN nor DOPPLER_TOKEN is set; "
            "cannot fetch CLAUDE_CREDENTIALS"
        )
    storage = DopplerStorage(
        token=token,
        project=project,
        config=config,
        secret_name=CLAUDE_CREDENTIALS_SECRET,
        local_path=credentials_path(),
    )
    storage.pull_to_local()
    return storage
