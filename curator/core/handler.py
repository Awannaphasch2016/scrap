"""Unified Lambda entrypoint for any curator topic.

Bootstraps the runtime environment that every curator topic needs · Doppler
secret fetch, /tmp HOME, Tailscale userspace networking + SOCKS5 → laptop
tinyproxy bridge, egress-IP logging · then dispatches to the topic selected
by the CURATOR_TOPIC env var (defaults to "scraping" for the legacy news
function).

Egress modes (resolved at cold-start):
  - HTTP_PROXY already set → respected as-is (e.g. vendor residential proxy).
  - HTTP_PROXY unset, TS_AUTHKEY + LAPTOP_TAILNET_IP set → bring up tailscaled
    in userspace networking mode and a localhost:8889 → SOCKS5 → laptop
    tinyproxy bridge.
  - Neither set → direct egress from AWS IPs. Reddit / job sites will return
    403; useful only for local smoke testing of the non-egress paths.

If the laptop is offline when the cron fires, _start_tailscale() returns
quickly on auth success but the first fetch through the bridge will time
out — CloudWatch records the error; the next day's run resumes once the
laptop is back online.

Topic dispatch:
  CURATOR_TOPIC=scraping → curator.topics.news.main()
  CURATOR_TOPIC=jobs     → curator.topics.jobs.main()
  Default (unset)        → scraping (preserves pre-refactor behavior of the
                            scrape-news-curator function which had no env var).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from importlib import import_module

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DOPPLER_DOWNLOAD_URL = (
    "https://api.doppler.com/v3/configs/config/secrets/download?format=json"
)

TAILSCALED_SOCKET = "/tmp/tailscaled.sock"
TAILSCALED_STATE = "/tmp/tailscaled.state"
TAILSCALED_SOCKS5_PORT = 1055
BRIDGE_LOCAL_PORT = 8889

# CURATOR_TOPIC env value → curator.topics.<module> name.
TOPIC_DISPATCH = {
    "scraping": "news",
    "jobs": "jobs",
}


def _fetch_one_doppler_config(token: str, label: str) -> int:
    """Pull all non-AWS_* / non-DOPPLER_* secrets from one Doppler config and
    inject into os.environ. Returns count of secrets imported.

    AWS_* skip: AWS_ACCESS_KEY_ID etc. are laptop deploy creds; the Lambda
    has its own execution role and setting AWS_* in env would OVERRIDE
    that role with empty/wrong creds, causing InvalidToken on boto3 calls.
    """
    req = urllib.request.Request(
        DOPPLER_DOWNLOAD_URL,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        secrets = json.load(r)

    n = 0
    for k, v in secrets.items():
        if k.startswith("DOPPLER_") or k.startswith("AWS_"):
            continue
        if not isinstance(v, str):
            continue
        os.environ[k] = v
        n += 1
    logger.info("fetched %d secrets from Doppler [%s]", n, label)
    return n


def _fetch_doppler_secrets() -> None:
    """Pull from the topic's own Doppler config (DOPPLER_TOKEN → scrape/dev)
    AND, when present, from the shared assistant-agent/dev config that holds
    the cross-project Supabase credentials (DOPPLER_ASSISTANT_AGENT_TOKEN →
    assistant-agent/dev).

    Order matters · scrape/dev first, assistant-agent/dev second. Later
    fetches OVERWRITE earlier ones on key collision (so if both configs
    define the same key, assistant-agent wins). In practice the configs
    don't overlap.
    """
    primary = os.environ.get("DOPPLER_TOKEN")
    if not primary:
        logger.warning("DOPPLER_TOKEN not set; skipping primary Doppler fetch")
    else:
        try:
            _fetch_one_doppler_config(primary, "scrape/dev")
        except Exception as e:  # noqa: BLE001
            logger.warning("primary doppler fetch failed: %s", e)

    secondary = os.environ.get("DOPPLER_ASSISTANT_AGENT_TOKEN")
    if secondary:
        try:
            _fetch_one_doppler_config(secondary, "assistant-agent/dev")
        except Exception as e:  # noqa: BLE001
            logger.warning("assistant-agent doppler fetch failed (non-fatal): %s", e)


def _prepare_writable_layout() -> None:
    """Lambda only allows writes to /tmp. Point HOME and CWD there."""
    os.environ["HOME"] = "/tmp"
    os.makedirs("/tmp", exist_ok=True)
    os.chdir("/tmp")


def _start_tailscale() -> None:
    """Spawn tailscaled in userspace mode and join the tailnet using TS_AUTHKEY."""
    authkey = os.environ.get("TS_AUTHKEY")
    if not authkey:
        logger.warning("TS_AUTHKEY not set; skipping tailscale start")
        return

    import subprocess
    import time

    subprocess.Popen(
        [
            "tailscaled",
            "--tun=userspace-networking",
            f"--socks5-server=localhost:{TAILSCALED_SOCKS5_PORT}",
            f"--socket={TAILSCALED_SOCKET}",
            f"--state={TAILSCALED_STATE}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    for _ in range(50):
        if os.path.exists(TAILSCALED_SOCKET):
            break
        time.sleep(0.2)
    else:
        raise RuntimeError("tailscaled control socket did not appear within 10s")

    # Hostname uses the Lambda function name (set by the runtime) so each
    # topic shows up as a distinct tailnet node in `tailscale status`.
    hostname = f"{os.environ.get('AWS_LAMBDA_FUNCTION_NAME', 'curator')}-lambda"

    # check=False on purpose · CalledProcessError's repr includes the full
    # argv, which contains the auth key. Don't leak it into CloudWatch.
    up = subprocess.run(
        [
            "tailscale",
            f"--socket={TAILSCALED_SOCKET}",
            "up",
            f"--authkey={authkey}",
            f"--hostname={hostname}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if up.returncode != 0:
        stderr_tail = (up.stderr.decode().strip() if up.stderr else "").splitlines()
        last = stderr_tail[-1] if stderr_tail else "no stderr"
        raise RuntimeError(f"tailscale up failed (exit {up.returncode}): {last}")
    logger.info("tailscaled up; SOCKS5 server on localhost:%d", TAILSCALED_SOCKS5_PORT)


def _wait_for_tailnet_peer(peer_ip: str, timeout_s: int = 20) -> None:
    """Block until tailscaled can reach the given tailnet IP, or timeout.

    `tailscale up` returns once the node has registered with the control plane,
    but learning OTHER peers' addresses + completing WireGuard handshake is
    asynchronous. Without this gate, the first SOCKS5 CONNECT after `up` often
    fails with `0x01 General SOCKS server failure`.
    """
    import subprocess
    import time

    deadline = time.time() + timeout_s
    last_err: str | None = None
    while time.time() < deadline:
        try:
            subprocess.run(
                [
                    "tailscale", f"--socket={TAILSCALED_SOCKET}",
                    "ping", "--c=1", "--timeout=3s", peer_ip,
                ],
                check=True, capture_output=True, timeout=5,
            )
            logger.info("tailnet peer %s reachable", peer_ip)
            return
        except subprocess.CalledProcessError as e:
            last_lines = (e.stderr.decode().strip() if e.stderr else "").splitlines()[-1:]
            last_err = last_lines[0] if last_lines else "ping failed"
        except subprocess.TimeoutExpired:
            last_err = "ping cmd timeout"
        time.sleep(0.5)
    logger.warning(
        "tailnet peer %s not reachable after %ds: %s — proceeding anyway",
        peer_ip, timeout_s, last_err or "unknown",
    )


def _start_proxy_bridge(local_port: int, laptop_ip: str, laptop_port: int) -> None:
    """Listen on localhost:<local_port> and forward each connection through SOCKS5.

    Architecture: `requests.get(..., proxies={'http': localhost:<local_port>})`
    hands HTTP-proxy bytes (CONNECT or full-URL GET) to this listener. We open
    a SOCKS5 socket via tailscaled (:1055) targeting (laptop_ip, laptop_port) —
    tinyproxy on the laptop receives the HTTP-proxy bytes natively and forwards
    using the laptop's residential NIC.
    """
    import socket
    import threading

    import socks  # pysocks

    def _splice(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                buf = src.recv(65536)
                if not buf:
                    break
                dst.sendall(buf)
        except OSError:
            pass
        finally:
            for s, how in ((src, socket.SHUT_RD), (dst, socket.SHUT_WR)):
                try:
                    s.shutdown(how)
                except OSError:
                    pass

    def _handle(client: socket.socket) -> None:
        try:
            upstream = socks.socksocket()
            upstream.set_proxy(socks.SOCKS5, "localhost", TAILSCALED_SOCKS5_PORT)
            upstream.connect((laptop_ip, laptop_port))
        except Exception as e:  # noqa: BLE001
            logger.warning("bridge upstream connect failed: %s", e)
            client.close()
            return
        threading.Thread(target=_splice, args=(client, upstream), daemon=True).start()
        threading.Thread(target=_splice, args=(upstream, client), daemon=True).start()

    def _accept_loop(srv: socket.socket) -> None:
        while True:
            try:
                client, _ = srv.accept()
            except OSError as e:
                logger.error("bridge accept failed: %s", e)
                return
            threading.Thread(target=_handle, args=(client,), daemon=True).start()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", local_port))
    srv.listen(16)
    threading.Thread(target=_accept_loop, args=(srv,), daemon=True).start()
    logger.info(
        "proxy bridge listening on localhost:%d → SOCKS5 :%d → %s:%d",
        local_port, TAILSCALED_SOCKS5_PORT, laptop_ip, laptop_port,
    )


def _log_egress_ip() -> None:
    """Best-effort: fetch api.ipify.org through the configured proxy and log it."""
    try:
        import requests  # noqa: PLC0415

        proxies = (
            {"http": os.environ["HTTP_PROXY"], "https": os.environ["HTTPS_PROXY"]}
            if os.environ.get("HTTP_PROXY") else None
        )
        ip = requests.get("https://api.ipify.org", proxies=proxies, timeout=8).text.strip()
        logger.info("egress IP: %s (proxied=%s)", ip, proxies is not None)
    except Exception as e:  # noqa: BLE001
        logger.warning("egress IP check failed: %s", e)


def _configure_egress() -> None:
    """Resolve which egress strategy to use based on env, after Doppler injection."""
    if os.environ.get("HTTP_PROXY"):
        logger.info("HTTP_PROXY already set; using vendor proxy as-is")
        return

    laptop_ip = os.environ.get("LAPTOP_TAILNET_IP")
    if not (os.environ.get("TS_AUTHKEY") and laptop_ip):
        logger.warning(
            "no proxy configured (TS_AUTHKEY/LAPTOP_TAILNET_IP missing); direct AWS egress"
        )
        return

    _start_tailscale()
    _wait_for_tailnet_peer(laptop_ip)
    _start_proxy_bridge(
        local_port=BRIDGE_LOCAL_PORT,
        laptop_ip=laptop_ip,
        laptop_port=int(os.environ.get("TINYPROXY_PORT", "8888")),
    )
    proxy_url = f"http://localhost:{BRIDGE_LOCAL_PORT}"
    os.environ["HTTP_PROXY"] = proxy_url
    os.environ["HTTPS_PROXY"] = proxy_url


def _resolve_topic_module() -> str:
    topic = os.environ.get("CURATOR_TOPIC", "scraping")
    module_name = TOPIC_DISPATCH.get(topic)
    if not module_name:
        raise RuntimeError(
            f"Unknown CURATOR_TOPIC={topic!r}; valid: {sorted(TOPIC_DISPATCH)}"
        )
    return f"curator.topics.{module_name}"


def _configure_schema_exposure(schemas: str) -> dict:
    """One-shot ops branch · expose a set of Postgres schemas to PostgREST.

    Routes the SQL pair `ALTER ROLE authenticator SET pgrst.db_schemas …`
    + `NOTIFY pgrst, 'reload config'` through this Lambda's network position,
    which can reach Supabase Postgres on :5432 even when the laptop can't
    (residential ISP filters / Supabase pooler-steering). The Lambda already
    has SUPABASE_DATABASE_URL from the assistant-agent/dev Doppler config —
    same path the daily Supabase flush uses, no new secret.

    Caveat: Supabase's dashboard config service may overwrite role-level
    settings on next project-config save. Treat as quick-fix, not durable
    IaC. Dashboard's Settings → API → Exposed schemas remains canonical.
    """
    import psycopg2

    dsn = os.environ.get("SUPABASE_DATABASE_URL")
    if not dsn:
        raise RuntimeError("SUPABASE_DATABASE_URL not in env (Doppler fetch missing?)")
    if not schemas:
        raise RuntimeError("schemas payload empty · refusing to clear pgrst.db_schemas")

    logger.info("configuring pgrst.db_schemas → %r", schemas)
    with psycopg2.connect(dsn, connect_timeout=15) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "ALTER ROLE authenticator SET pgrst.db_schemas TO %s", (schemas,)
            )
            cur.execute("NOTIFY pgrst, 'reload config'")
            cur.execute(
                "SELECT rolconfig FROM pg_roles WHERE rolname = 'authenticator'"
            )
            rolconfig = cur.fetchone()[0]
    logger.info("rolconfig now: %s", rolconfig)
    return {"status": "ok", "schemas": schemas, "rolconfig": rolconfig}


def lambda_handler(event, context):  # noqa: ARG001
    _prepare_writable_layout()
    _fetch_doppler_secrets()

    action = (event or {}).get("action")
    if action == "configure-schema-exposure":
        # Skip egress + topic dispatch · Supabase is reachable from direct AWS
        # egress, so we don't need Tailscale or the residential proxy bridge.
        return _configure_schema_exposure(event.get("schemas", ""))

    _configure_egress()
    _log_egress_ip()

    topic_module = _resolve_topic_module()
    logger.info("dispatching to %s", topic_module)
    import_module(topic_module).main()
    return {"status": "ok"}
