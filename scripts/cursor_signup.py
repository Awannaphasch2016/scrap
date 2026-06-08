#!/usr/bin/env python3
"""Paperclip process-adapter wrapper for the Cursor signup → create-token chain.

State-routed dispatcher (rung 2). Same chassis as vercel_signup.py; swaps URL,
state vocab, dispatch table, and OAuth provider.

Differences from Vercel:
  - Uses Google OAuth (anakwannaphaschaiyong@gmail.com) instead of GitHub.
  - Cursor's auth lives on authenticator.cursor.sh (separate subdomain).
  - Adds at_dashboard_other state for cases where post-auth lands somewhere
    other than the API keys page; dispatches inline-navigate to /dashboard/api-keys.
  - Token format: cursor_sk_* or key_* (vision extraction handles either).
  - Includes background screen recording — saves frames to
    /tmp/cursor_recording_<ts>/ so the user can review post-mortem.

Invoke as:
  doppler run --project accounts --config oauth -- python scripts/cursor_signup.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from state_classifier import classify, CURSOR_STATES   # noqa: E402


URL = os.environ.get("CURSOR_URL", "https://cursor.com/dashboard")
REPO = Path(__file__).parent.parent
SESSION = os.environ.get("CURSOR_SESSION", "cursor")
# Cursor tokens observed in the wild use `crsr_` prefix (empirically
# confirmed 2026-06-07 from classifier evidence). Other patterns possible
# for older accounts: `cursor_sk_*`, `key_*`. Vision extraction reads the
# literal; this regex sanity-checks shape.
KEY_REGEX = re.compile(r"^(?:crsr_|cursor_sk_|key_)[a-zA-Z0-9]{20,}$")

MAX_OUTER_ITER = 10
STATE_LOOP_GUARD = 3
INITIAL_SETTLE_S = 5

# Cursor uses Google OAuth for this identity (anakwannaphaschaiyong@gmail.com,
# in doppler accounts/oauth as GOOGLE_OAUTH_EMAIL).
os.environ.setdefault("OAUTH_PROVIDER", "google")

# Background screen recording — saves /tmp/cursor_recording_<ts>/frame_NNNNN.png
# Set CURSOR_RECORD=0 to disable.
RECORD = os.environ.get("CURSOR_RECORD", "1") != "0"
RECORD_DIR = Path(f"/tmp/cursor_recording_{int(time.time())}")
RECORD_FPS = float(os.environ.get("CURSOR_RECORD_FPS", "1.0"))


def _run(cmd: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess:
    if cmd[:2] == ["opencli", "browser"] and "--window" not in cmd:
        cmd = cmd[:3] + ["--window", "background"] + cmd[3:]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# ─── opencli helpers ──────────────────────────────────────────────────────────


def session_tab_url(session: str) -> str | None:
    r = _run(["opencli", "browser", session, "tab", "list"])
    if r.returncode != 0:
        return None
    cut = r.stdout.find("\n  Update available:")
    out = r.stdout[:cut].rstrip() if cut != -1 else r.stdout.rstrip()
    try:
        tabs = json.loads(out)
    except json.JSONDecodeError:
        return None
    return tabs[0].get("url", "") if tabs else None


def ensure_tab_for_cold_start(session: str, url: str) -> None:
    current = session_tab_url(session)
    is_blank = current is None or current.startswith(("about:", "chrome://newtab"))
    if current is None:
        action = ["opencli", "browser", session, "tab", "new", url]
    elif is_blank:
        action = ["opencli", "browser", session, "open", url]
    else:
        print(f"[wrapper] preserving existing tab at {current[:80]}",
              file=sys.stderr)
        return
    r = _run(action, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(action[:5])} failed: {r.stderr[:200]}")
    print(f"[wrapper] cold-start navigated to {url}", file=sys.stderr)
    time.sleep(INITIAL_SETTLE_S)


# ─── sub-agent dispatch ───────────────────────────────────────────────────────


def run_sub(name: str, script: str, *args: str) -> tuple[str, dict | None]:
    rc = subprocess.run([sys.executable, script, *args], cwd=REPO).returncode
    if rc != 0:
        return f"crashed_exit_{rc}", None
    trace_dir = Path(f"/tmp/{name}")
    traces = sorted(trace_dir.glob("*.json"),
                    key=lambda p: p.stat().st_mtime, reverse=True)
    if not traces:
        return "no_trace_written", None
    trace = json.loads(traces[0].read_text())
    return trace.get("outcome", "missing_outcome_field"), trace


def _dispatch_signup(session: str, url: str) -> tuple[str, dict | None]:
    return run_sub("signup_agent", "scripts/signup_agent.py", url,
                   "--session", session)


def _dispatch_signup_resume(session: str, url: str) -> tuple[str, dict | None]:
    return run_sub("signup_agent", "scripts/signup_agent.py",
                   "--no-nav", "--session", session)


def _dispatch_onboarding(session: str, url: str) -> tuple[str, dict | None]:
    return run_sub("onboarding_doer", "scripts/onboarding_doer_agent.py",
                   "--no-nav", "--session", session)


def _dispatch_create_token(session: str, url: str) -> tuple[str, dict | None]:
    return run_sub("create_token", "scripts/create_token_agent.py",
                   "--no-nav", "--session", session)


def _dispatch_extract_direct(session: str, url: str) -> tuple[str, dict | None]:
    """at_token_revealed — use vision (claude -p with @<path>) to read the
    token literal off a fresh screenshot. Cursor likely renders tokens
    inside opaque <pre> elements (same pattern as Vercel)."""
    shot = Path(f"/tmp/cursor_extract_{int(time.time())}.png")
    cap = _run(["opencli", "browser", session, "screenshot", str(shot)])
    if cap.returncode != 0:
        return "screenshot_failed", None
    prompt = (
        f"@{shot} Read the literal API key visible on this screen. Cursor "
        f"API keys start with 'crsr_' (most common) or 'cursor_sk_' or "
        f"'key_' followed by 20+ alphanumeric characters. It will be "
        f"displayed inside a 'User API Key Created' modal or similar "
        f"panel showing the just-created key, typically with a 'Copy' "
        f"button and a one-time-show warning. Reply with ONLY the key "
        f"literal — no quotes, no labels, no explanation. If you cannot "
        f"see a key matching that pattern, reply with exactly "
        f"'NO_TOKEN_VISIBLE'."
    )
    r = subprocess.run(["claude", "-p"], input=prompt,
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return f"claude_err_{r.returncode}", None
    token = r.stdout.strip().strip("'\"`").strip()
    if KEY_REGEX.match(token):
        return "done", {"trace": [{"action": {"value": token}}]}
    return "no_match", None


def _dispatch_navigate_to_api_keys(session: str, url: str) -> tuple[str, dict | None]:
    """at_dashboard_other — click the 'API Keys' sidebar link via path-2
    (opencli's native --text flag), since Cursor's React Router ignores
    direct URL open() calls. Fall back to URL nav if the click fails.

    Validated empirically 2026-06-07: opencli's `open` command navigates but
    Cursor's SPA may redirect back to /dashboard root; clicking the actual
    sidebar link is the only reliable way to advance.
    """
    # Path-2 click by visible text — the sidebar nav has 'API Keys' as link text
    r = _run(["opencli", "browser", session, "click", "--text", "API Keys"],
             timeout=30)
    if r.returncode == 0:
        time.sleep(INITIAL_SETTLE_S)
        return "done", {"trace": [{"action": {"value": "clicked_api_keys_link"}}]}

    # Fallback: try clicking by title attribute (Cursor sidebar uses title=)
    r2 = _run(["opencli", "browser", session, "click",
               "a[title='API Keys']"], timeout=30)
    if r2.returncode == 0:
        time.sleep(INITIAL_SETTLE_S)
        return "done", {"trace": [{"action": {"value": "clicked_api_keys_link_css"}}]}

    # Last resort: URL navigation
    r3 = _run(["opencli", "browser", session, "open",
               "https://cursor.com/dashboard/api-keys"], timeout=30)
    if r3.returncode == 0:
        time.sleep(INITIAL_SETTLE_S)
        return "done", {"trace": [{"action": {"value": "navigated_via_url"}}]}

    return ("nav_all_paths_failed",
            {"trace": [{"action": {"value": f"click_text_rc={r.returncode}, "
                                            f"css_rc={r2.returncode}, "
                                            f"open_rc={r3.returncode}"}}]})


STATE_DISPATCH = {
    "at_signup_or_login":  _dispatch_signup,
    "in_google_oauth":     _dispatch_signup_resume,
    "in_onboarding":       _dispatch_onboarding,
    "at_dashboard_other":  _dispatch_navigate_to_api_keys,
    "at_api_keys_page":    _dispatch_create_token,
    "at_token_revealed":   _dispatch_extract_direct,
}


# ─── disposition ──────────────────────────────────────────────────────────────


def declare(status: str) -> None:
    api_url = os.environ.get("PAPERCLIP_API_URL")
    agent_id = os.environ.get("PAPERCLIP_AGENT_ID")
    company_id = os.environ.get("PAPERCLIP_COMPANY_ID")
    if not (api_url and agent_id and company_id):
        print("[declare] PAPERCLIP env vars absent — skip (standalone run)",
              file=sys.stderr)
        return
    headers = {"Content-Type": "application/json"}
    if api_key := os.environ.get("PAPERCLIP_API_KEY"):
        headers["Authorization"] = f"Bearer {api_key}"
    if run_id := os.environ.get("PAPERCLIP_RUN_ID"):
        headers["X-Paperclip-Run-Id"] = run_id
    list_url = (f"{api_url}/api/companies/{company_id}/issues"
                f"?assigneeAgentId={agent_id}&status=in_progress")
    try:
        with urllib.request.urlopen(list_url, timeout=10) as resp:
            issues = json.loads(resp.read().decode())
        issues = issues if isinstance(issues, list) else issues.get("items", [])
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"[declare] couldn't fetch issues: {e}", file=sys.stderr)
        return
    if not issues:
        print("[declare] no in_progress issue assigned — skip", file=sys.stderr)
        return
    issue = issues[0]
    issue_status = "blocked" if status == "failed" else status
    req = urllib.request.Request(
        f"{api_url}/api/issues/{issue['id']}",
        method="PATCH",
        data=json.dumps({"status": issue_status}).encode(),
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"[declare] PATCH /api/issues/{issue.get('identifier')} "
                  f"status={issue_status} → HTTP {resp.status}", file=sys.stderr)
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"[declare] PATCH failed: {e}", file=sys.stderr)


# ─── main loop ────────────────────────────────────────────────────────────────


def terminal(status: str, summary: str, key: str = "") -> None:
    declare(status)
    print(json.dumps({
        "status": status, "tool": "cursor", "api_key": key, "summary": summary,
    }))
    sys.exit(0 if status in ("done", "blocked") else 1)


def main() -> None:
    print(f"[wrapper] state-routed dispatcher · session={SESSION} url={URL}",
          file=sys.stderr)
    print(f"[wrapper] caps · max_outer_iter={MAX_OUTER_ITER} "
          f"state_loop_guard={STATE_LOOP_GUARD}", file=sys.stderr)

    recorder = None
    if RECORD:
        RECORD_DIR.mkdir(parents=True, exist_ok=True)
        recorder = subprocess.Popen([
            sys.executable, "scripts/screen_recorder.py",
            "--session", SESSION,
            "--output-dir", str(RECORD_DIR),
            "--fps", str(RECORD_FPS),
            "--max-frames", "900",
        ])
        print(f"[wrapper] recording to {RECORD_DIR} @ {RECORD_FPS} fps "
              f"(pid={recorder.pid})", file=sys.stderr)

    try:
        ensure_tab_for_cold_start(SESSION, URL)

        classification_trace: list[dict] = []
        consecutive_same_state = 0
        last_state = None

        for outer in range(1, MAX_OUTER_ITER + 1):
            try:
                cls = classify(SESSION, states=CURSOR_STATES)
            except Exception as e:
                print(f"[wrapper] classifier error: {e}", file=sys.stderr)
                terminal("failed", f"classifier error at iter {outer}: {e}")

            state = cls["state"]
            classification_trace.append({"iter": outer, **cls})
            print(f"[wrapper] iter {outer}/{MAX_OUTER_ITER}  state={state}  "
                  f"evidence={cls['evidence'][:120]!r}", file=sys.stderr)

            if state == last_state:
                consecutive_same_state += 1
            else:
                consecutive_same_state = 1
            last_state = state

            if consecutive_same_state >= STATE_LOOP_GUARD:
                terminal(
                    "failed",
                    f"state-loop guard fired: state={state!r} repeated "
                    f"{consecutive_same_state} consecutive iterations",
                )

            if state == "blocked_external_gate":
                terminal("blocked", f"external gate: {cls['evidence']}")
            if state == "unknown":
                terminal("failed",
                         f"classifier returned unknown at iter {outer}: "
                         f"{cls['evidence']}")

            dispatch = STATE_DISPATCH.get(state)
            if dispatch is None:
                terminal("failed", f"no dispatch for state {state!r}")

            outcome, trace = dispatch(SESSION, URL)
            print(f"[wrapper]   sub-agent outcome: {outcome}", file=sys.stderr)

            # Token extraction — same logic as Vercel
            if state in ("at_api_keys_page", "at_token_revealed") and \
                    outcome == "done" and trace:
                last_action = (trace.get("trace") or [{}])[-1].get("action") or {}
                value = (last_action.get("value") or "").strip()
                if KEY_REGEX.match(value):
                    terminal("done",
                             f"extracted via {state} dispatch at iter {outer}",
                             key=value)
                # Immediate vision extraction (catches narrow reveal window)
                print(f"[wrapper]   running immediate vision extraction "
                      f"(marker={value!r})", file=sys.stderr)
                vision_outcome, vision_trace = _dispatch_extract_direct(SESSION, URL)
                print(f"[wrapper]   vision extraction outcome: {vision_outcome}",
                      file=sys.stderr)
                if vision_outcome == "done" and vision_trace:
                    vkey = (vision_trace["trace"][-1]["action"]["value"] or "").strip()
                    if KEY_REGEX.match(vkey):
                        terminal("done",
                                 f"extracted via immediate vision after "
                                 f"{state} dispatch at iter {outer}",
                                 key=vkey)

        terminal("failed",
                 f"max_outer_iter={MAX_OUTER_ITER} exceeded; "
                 f"trace: {[c['state'] for c in classification_trace]}")

    finally:
        if recorder is not None:
            recorder.terminate()
            try:
                recorder.wait(timeout=5)
            except subprocess.TimeoutExpired:
                recorder.kill()
            print(f"[wrapper] recording saved to {RECORD_DIR}", file=sys.stderr)
            print(f"[wrapper] stitch with: ffmpeg -framerate {RECORD_FPS} "
                  f"-i {RECORD_DIR}/frame_%05d.png -c:v libx264 -pix_fmt "
                  f"yuv420p {RECORD_DIR}.mp4", file=sys.stderr)


if __name__ == "__main__":
    main()
