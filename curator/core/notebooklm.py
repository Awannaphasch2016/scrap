"""NotebookLM upload + Doppler-backed cookie rotation, shared across topics.

Each topic uploads its rotating digest as a single named source on its own
notebook (so `<topic> ask` chats against a fresh corpus). The upload path
needs an authenticated `storage_state.json` cookie blob; production Lambdas
keep that blob in Doppler so cookie rotations done by one run are visible
to the next without redeploy.

Two-clock auth:
  1. Server-side session — kept warm by daily activity.
  2. Cookie-value TTL — rotates per session; if the rotated cookie isn't
     persisted back to Doppler, the blob staled out and the next cold-start
     reads expired cookies.

`DopplerStorage.maybe_push_back()` is the persistence half. See the
`notebooklm-grounded-chat` pattern for the full theory.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

# Local materialization target for the storage_state cookie blob. Lives under
# $HOME so the Lambda handler can redirect HOME=/tmp once and never need a
# code change here.
NOTEBOOKLM_STORAGE_PATH = Path.home() / ".notebooklm" / "storage_state.json"
NOTEBOOKLM_STORAGE_SECRET = "NOTEBOOKLM_STORAGE_STATE"


def _ensure_notebooklm_storage() -> None:
    """Legacy/fallback: materialize NOTEBOOKLM_STORAGE_STATE env onto disk if missing.

    Used when no Doppler write-token is available (no push-back path). With a
    write-token, DopplerStorage.pull_to_local() replaces this on every upload —
    that path is preferred because it picks up cookie rotations pushed by other
    runs (or by the laptop) without redeploy.
    """
    if NOTEBOOKLM_STORAGE_PATH.exists() and NOTEBOOKLM_STORAGE_PATH.stat().st_size > 0:
        return
    blob = os.environ.get(NOTEBOOKLM_STORAGE_SECRET)
    if not blob:
        return
    NOTEBOOKLM_STORAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    NOTEBOOKLM_STORAGE_PATH.write_text(blob, encoding="utf-8")
    os.chmod(NOTEBOOKLM_STORAGE_PATH, 0o600)


class DopplerStorage:
    """Pull/push storage_state.json to a Doppler secret.

    Two-clock auth (server-side session + cookie-value TTL) requires capturing
    rotated cookies and putting them where the next cold-start can read them.
    Without push-back, the cookie value TTL eventually elapses regardless of
    how often the cron runs. See `notebooklm-grounded-chat` pattern.

    Push-back requires a token with read/write scope on the Doppler config —
    DOPPLER_WRITE_TOKEN if set, falling back to DOPPLER_TOKEN. If the token is
    read-only, push() will 403 and we log a warning (never raise).
    """

    _API_BASE = "https://api.doppler.com/v3"

    def __init__(
        self,
        token: str,
        project: str,
        config: str,
        secret_name: str,
        local_path: Path,
    ) -> None:
        self._token = token
        self._project = project
        self._config = config
        self._secret_name = secret_name
        self._local_path = local_path
        self._last_known: str | None = None

    def pull_to_local(self) -> None:
        """Fetch from Doppler API, write to local_path, cache as last_known."""
        params = urllib.parse.urlencode(
            {"project": self._project, "config": self._config, "name": self._secret_name}
        )
        url = f"{self._API_BASE}/configs/config/secret?{params}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
        value = data["value"]["computed"]
        self._local_path.parent.mkdir(parents=True, exist_ok=True)
        self._local_path.write_text(value, encoding="utf-8")
        os.chmod(self._local_path, 0o600)
        self._last_known = value
        logger.info(
            "doppler: pulled %s (%d bytes) → %s",
            self._secret_name, len(value), self._local_path,
        )

    def maybe_push_back(self) -> bool:
        """Push local file to Doppler if it differs from last_known. Never raises.

        Returns True if a push was issued, False if no-op (file unchanged or
        push failed). Both branches are non-fatal — the worst case is one
        rotated cookie not making it back to Doppler, which costs us one
        cookie-TTL cycle of catch-up.
        """
        try:
            if not self._local_path.exists():
                return False
            current = self._local_path.read_text(encoding="utf-8")
            if self._last_known is not None and current == self._last_known:
                logger.info("doppler: no rotation detected; skipping push-back")
                return False
            body = json.dumps(
                {
                    "project": self._project,
                    "config": self._config,
                    "secrets": {self._secret_name: current},
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                f"{self._API_BASE}/configs/config/secrets",
                data=body,
                method="POST",
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                _ = r.read()
            self._last_known = current
            logger.info(
                "doppler: pushed rotated %s (%d bytes)",
                self._secret_name, len(current),
            )
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("doppler push-back failed (non-fatal): %s", e)
            return False


def _doppler_storage_or_none() -> "DopplerStorage | None":
    """Build a DopplerStorage if a token is available, else None.

    Prefer DOPPLER_WRITE_TOKEN (explicitly write-scoped); fall back to
    DOPPLER_TOKEN (may or may not have write — push attempt will tell us).
    """
    token = os.environ.get("DOPPLER_WRITE_TOKEN") or os.environ.get("DOPPLER_TOKEN")
    if not token:
        return None
    return DopplerStorage(
        token=token,
        project=os.environ.get("DOPPLER_PROJECT", "scrape"),
        config=os.environ.get("DOPPLER_CONFIG", "dev"),
        secret_name=NOTEBOOKLM_STORAGE_SECRET,
        local_path=NOTEBOOKLM_STORAGE_PATH,
    )


class NotebookLMUploader:
    """Uploads a local file to a NotebookLM notebook. Lazy-imports notebooklm-py.

    Requires:
      - `pip install "notebooklm-py[browser]"`
      - Either a local `~/.notebooklm/storage_state.json` (from `notebooklm login`)
        or NOTEBOOKLM_STORAGE_STATE env var (e.g. injected via `doppler run`),
        with a DOPPLER_TOKEN env var (read+write scope) for push-back of
        cookies rotated during the session.
      - A notebook ID passed in by the topic (NOTEBOOKLM_NOTEBOOK_ID for news,
        JOBS_NOTEBOOK_ID for jobs).
    """

    def __init__(self, notebook_id: str):
        self.notebook_id = notebook_id
        self._doppler = _doppler_storage_or_none()

    def upload(self, file_path: str) -> None:
        import asyncio

        # Pull on entry: prefer Doppler-as-source-of-truth so we pick up
        # cookies pushed by other runs or by the laptop without redeploy.
        # Fall back to env-blob materialization if Doppler is unavailable.
        if self._doppler is not None:
            try:
                self._doppler.pull_to_local()
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "doppler pull failed (%s); falling back to env-blob materialization", e
                )
                _ensure_notebooklm_storage()
        else:
            _ensure_notebooklm_storage()

        from notebooklm import NotebookLMClient  # lazy import; optional dep

        target_title = Path(file_path).name

        async def _run() -> None:
            async with await NotebookLMClient.from_storage() as client:
                # rotate: delete any prior source with the same filename so the
                # notebook holds exactly one current digest, not one per day
                existing = await client.sources.list(self.notebook_id)
                for s in existing:
                    if getattr(s, "title", "") == target_title:
                        try:
                            await client.sources.delete(self.notebook_id, s.id)
                        except Exception as e:  # noqa: BLE001
                            print(f"  warning: could not delete prior {target_title}: {e}")
                await client.sources.add_file(self.notebook_id, file_path, wait=True)

        asyncio.run(_run())

        # Push-back: capture the rotated cookie notebooklm-py wrote to disk
        # during the session so the next cron run starts with fresh credentials.
        # Without this, cookie value TTL elapses on Doppler's stale blob even
        # though daily activity warms the server-side session timer.
        if self._doppler is not None:
            self._doppler.maybe_push_back()
