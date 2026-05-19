"""Lambda entrypoint for the scraping news curator.

Fetches secrets from Doppler (using DOPPLER_TOKEN), redirects HOME/CWD to /tmp
(only writable path in Lambda), brings up Tailscale + an in-process HTTP→SOCKS5
proxy bridge so Reddit fetches egress through the operator's residential ISP IP,
then runs the curator's main pipeline.

Egress modes (resolved at cold-start):
  - HTTP_PROXY already set (e.g. vendor residential proxy like Webshare) → respected as-is.
  - HTTP_PROXY unset, TS_AUTHKEY + LAPTOP_TAILNET_IP set → bring up tailscaled in
    userspace networking mode and a localhost:8889 → SOCKS5 → laptop tinyproxy bridge.
  - Neither set → direct egress from AWS IPs. Reddit will return 403; useful only for
    local dev / smoke testing the non-egress paths.

If the laptop is offline when the cron fires, _start_tailscale() returns quickly
on auth success but the first fetch through the bridge will time out — CloudWatch
records the error; the next day's run resumes once the laptop is back online.
"""

import json
import logging
import os
import urllib.request

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DOPPLER_DOWNLOAD_URL = "https://api.doppler.com/v3/configs/config/secrets/download?format=json"

TAILSCALED_SOCKET = "/tmp/tailscaled.sock"
TAILSCALED_STATE = "/tmp/tailscaled.state"
TAILSCALED_SOCKS5_PORT = 1055
BRIDGE_LOCAL_PORT = 8889


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


def _start_tailscale() -> None:
    """Spawn tailscaled in userspace mode and join the tailnet using TS_AUTHKEY.

    No --exit-node flag — Lambda's userspace networking fails the rp_filter
    sanity check and sysctl is not writable in the sandbox (Tailscale #14409).
    Instead we expose a SOCKS5 server on localhost:1055 that _start_proxy_bridge
    forwards through.
    """
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

    subprocess.run(
        [
            "tailscale",
            f"--socket={TAILSCALED_SOCKET}",
            "up",
            f"--authkey={authkey}",
            "--hostname=scrape-news-curator-lambda",
        ],
        check=True,
        timeout=30,
    )
    logger.info("tailscaled up; SOCKS5 server on localhost:%d", TAILSCALED_SOCKS5_PORT)


def _start_proxy_bridge(local_port: int, laptop_ip: str, laptop_port: int) -> None:
    """Listen on localhost:<local_port> and forward each connection through SOCKS5.

    Architecture: `requests.get(..., proxies={'http': localhost:<local_port>})`
    hands HTTP-proxy bytes (CONNECT or full-URL GET) to this listener. We open
    a SOCKS5 socket via tailscaled (:1055) targeting (laptop_ip, laptop_port) —
    tinyproxy on the laptop receives the HTTP-proxy bytes natively and forwards
    to Reddit using the laptop's residential NIC. Two daemon threads splice
    bytes both directions per connection.
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
    """Best-effort: fetch api.ipify.org through the configured proxy and log it.

    Surfaces the actual egress IP in CloudWatch so we can confirm the residential
    chain is engaged (vs the Lambda having silently fallen back to AWS egress).
    """
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
        logger.warning("no proxy configured (TS_AUTHKEY/LAPTOP_TAILNET_IP missing); direct AWS egress")
        return

    _start_tailscale()
    _start_proxy_bridge(
        local_port=BRIDGE_LOCAL_PORT,
        laptop_ip=laptop_ip,
        laptop_port=int(os.environ.get("TINYPROXY_PORT", "8888")),
    )
    proxy_url = f"http://localhost:{BRIDGE_LOCAL_PORT}"
    os.environ["HTTP_PROXY"] = proxy_url
    os.environ["HTTPS_PROXY"] = proxy_url


def lambda_handler(event, context):  # noqa: ARG001
    _prepare_writable_layout()
    _fetch_doppler_secrets()
    _configure_egress()
    _log_egress_ip()

    from news_curator import main as run_curator

    run_curator()
    return {"status": "ok"}
