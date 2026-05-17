"""Lambda entrypoint for the scraping news curator.

Fetches secrets from Doppler (using DOPPLER_TOKEN), redirects HOME/CWD to /tmp
(only writable path in Lambda), then runs the curator's main pipeline.
"""

import json
import logging
import os
import urllib.request

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DOPPLER_DOWNLOAD_URL = "https://api.doppler.com/v3/configs/config/secrets/download?format=json"


def _fetch_doppler_secrets() -> None:
    """Pull secrets from Doppler config bound to DOPPLER_TOKEN and inject into env."""
    token = os.environ.get("DOPPLER_TOKEN")
    if not token:
        logger.warning("DOPPLER_TOKEN not set; skipping Doppler secret fetch")
        return

    req = urllib.request.Request(
        DOPPLER_DOWNLOAD_URL,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        secrets = json.load(r)

    n = 0
    for k, v in secrets.items():
        if k.startswith("DOPPLER_"):
            continue
        if not isinstance(v, str):
            continue
        os.environ[k] = v
        n += 1
    logger.info("fetched %d secrets from Doppler", n)


def _prepare_writable_layout() -> None:
    """Lambda only allows writes to /tmp. Point HOME and CWD there."""
    os.environ["HOME"] = "/tmp"
    os.makedirs("/tmp", exist_ok=True)
    os.chdir("/tmp")


def lambda_handler(event, context):  # noqa: ARG001
    _prepare_writable_layout()
    _fetch_doppler_secrets()

    from news_curator import main as run_curator

    run_curator()
    return {"status": "ok"}
