"""Lambda entrypoint for the job curator.

Same bootstrap shape as news handler (Doppler → /tmp HOME → Tailscale-fronted
residential proxy → run curator). Only differences:

  - Calls `from job_curator import main` instead of news_curator.
  - Reads JOBS_NOTEBOOK_ID / JOBS_FEED_BUCKET (set in Doppler) instead of the
    news-side equivalents · job_curator.py picks them up directly from os.environ.

Lambda handler-name override is set on the function (Image Config Command =
['job_handler.lambda_handler']) so both news and jobs can share the same image.
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
        if k.startswith("DOPPLER_") or not isinstance(v, str):
            continue
        os.environ[k] = v
        n += 1
    logger.info("fetched %d secrets from Doppler", n)


def _prepare_writable_layout() -> None:
    os.environ["HOME"] = "/tmp"
    os.makedirs("/tmp", exist_ok=True)
    os.chdir("/tmp")


def _start_tailscale() -> None:
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

    up = subprocess.run(
        [
            "tailscale", f"--socket={TAILSCALED_SOCKET}", "up",
            f"--authkey={authkey}", "--hostname=scrape-job-curator-lambda",
        ],
        check=False, capture_output=True, timeout=30,
    )
    if up.returncode != 0:
        stderr_tail = (up.stderr.decode().strip() if up.stderr else "").splitlines()
        last = stderr_tail[-1] if stderr_tail else "no stderr"
        raise RuntimeError(f"tailscale up failed (exit {up.returncode}): {last}")
    logger.info("tailscaled up; SOCKS5 on localhost:%d", TAILSCALED_SOCKS5_PORT)


def _wait_for_tailnet_peer(peer_ip: str, timeout_s: int = 20) -> None:
    import subprocess
    import time
    deadline = time.time() + timeout_s
    last_err = None
    while time.time() < deadline:
        try:
            subprocess.run(
                ["tailscale", f"--socket={TAILSCALED_SOCKET}",
                 "ping", "--c=1", "--timeout=3s", peer_ip],
                check=True, capture_output=True, timeout=5,
            )
            logger.info("tailnet peer %s reachable", peer_ip)
            return
        except subprocess.CalledProcessError as e:
            last_err = (e.stderr.decode().strip() if e.stderr else "").splitlines()[-1:]
            last_err = last_err[0] if last_err else "ping failed"
        except subprocess.TimeoutExpired:
            last_err = "ping cmd timeout"
        time.sleep(0.5)
    logger.warning("tailnet peer %s not reachable after %ds: %s — proceeding anyway",
                   peer_ip, timeout_s, last_err or "unknown")


def _start_proxy_bridge(local_port: int, laptop_ip: str, laptop_port: int) -> None:
    import socket
    import threading
    import socks

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
        except Exception as e:
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
    logger.info("proxy bridge on localhost:%d → SOCKS5 :%d → %s:%d",
                local_port, TAILSCALED_SOCKS5_PORT, laptop_ip, laptop_port)


def _log_egress_ip() -> None:
    try:
        import requests
        proxies = (
            {"http": os.environ["HTTP_PROXY"], "https": os.environ["HTTPS_PROXY"]}
            if os.environ.get("HTTP_PROXY") else None
        )
        ip = requests.get("https://api.ipify.org", proxies=proxies, timeout=8).text.strip()
        logger.info("egress IP: %s (proxied=%s)", ip, proxies is not None)
    except Exception as e:
        logger.warning("egress IP check failed: %s", e)


def _configure_egress() -> None:
    if os.environ.get("HTTP_PROXY"):
        logger.info("HTTP_PROXY already set; using vendor proxy as-is")
        return
    laptop_ip = os.environ.get("LAPTOP_TAILNET_IP")
    if not (os.environ.get("TS_AUTHKEY") and laptop_ip):
        logger.warning("no proxy configured (TS_AUTHKEY/LAPTOP_TAILNET_IP missing); direct AWS egress")
        return
    _start_tailscale()
    _wait_for_tailnet_peer(laptop_ip)
    _start_proxy_bridge(
        local_port=BRIDGE_LOCAL_PORT, laptop_ip=laptop_ip,
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

    from job_curator import main as run_curator
    run_curator()
    return {"status": "ok"}
