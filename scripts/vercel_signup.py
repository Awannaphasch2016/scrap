#!/usr/bin/env python3
"""Paperclip process-adapter wrapper for the Vercel signup → create-token chain.

State-routed dispatcher (rung 2). Same chassis as hyperbrowser_signup.py;
swaps URL + persistence target + classifier vocabulary + dispatch table.

Differences from Hyperbrowser:
  - Vercel's API tokens require explicit creation (not dashboard-visible).
  - signup_agent.py reused as-is because Vercel offers 'Continue with Google'.
  - New sub-agent: create_token_agent.py (find Create button, fill name, submit,
    extract revealed token).
  - State vocab VERCEL_STATES adds at_token_list_page and at_token_revealed.

Tab navigation strategy:
  Land directly at /account/settings/tokens. Vercel redirects to login with
  next=%2Faccount%2Fsettings%2Ftokens, and after OAuth drops us back at the
  tokens page — skipping the need for explicit /dashboard navigation.

Contract Paperclip sees:
  stdout final line: {"status": "done|blocked|failed", "tool": "vercel",
                      "api_key": "<token>", "summary": "<one-line reason>"}
  exit code:         0 for done/blocked, 1 for failed
  PATCH callback:    /api/issues/<assigned-issue-id> {status: ...}
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

# Make state_classifier importable when this file runs from anywhere.
sys.path.insert(0, str(Path(__file__).parent))
from state_classifier import classify, VERCEL_STATES   # noqa: E402


URL = os.environ.get("VERCEL_URL", "https://vercel.com/account/settings/tokens")
REPO = Path(__file__).parent.parent
SESSION = os.environ.get("VERCEL_SESSION", "vercel")
# Vercel personal API tokens use the `vcp_` prefix followed by 24+ alphanumeric
# chars. This is distinct from team IDs (`team_*`) and project IDs (`prj_*`)
# which also appear in the post-create modal's DOM — narrow the regex to avoid
# misidentifying them.
VERCEL_TOKEN_PREFIX = "vcp_"
KEY_REGEX = re.compile(rf"^{VERCEL_TOKEN_PREFIX}[a-zA-Z0-9]{{24,}}$")

# Vercel auth: this user's identity uses GitHub OAuth (per
# accounts/oauth doppler config — GITHUB_OAUTH_EMAIL).
# Invoke as: `doppler run --project accounts --config oauth -- python
# scripts/vercel_signup.py` so identity env vars are in the wrapper's
# environment and inherit through to sub-agent subprocesses.
os.environ.setdefault("OAUTH_PROVIDER", "github")

MAX_OUTER_ITER = 10
STATE_LOOP_GUARD = 3
INITIAL_SETTLE_S = 5

BLOCKED_SIGNALS = ("required", "captcha", "payment", "kyc", "verification",
                   "gate", "limit", "locked")


def _run(cmd: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess:
    # See agent_core._run — keep wrapper's opencli calls from stealing focus.
    if cmd[:2] == ["opencli", "browser"] and "--window" not in cmd:
        cmd = cmd[:3] + ["--window", "background"] + cmd[3:]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# ─── opencli helpers (lightweight; full chassis lives in agent_core) ──────────


def session_tab_url(session: str) -> str | None:
    """Return the current bound tab's URL, or None if session owns no tabs."""
    r = _run(["opencli", "browser", session, "tab", "list"])
    if r.returncode != 0:
        return None
    cut = r.stdout.find("\n  Update available:")
    out = r.stdout[:cut].rstrip() if cut != -1 else r.stdout.rstrip()
    try:
        tabs = json.loads(out)
    except json.JSONDecodeError:
        return None
    if not tabs:
        return None
    return tabs[0].get("url", "")


def ensure_tab_for_cold_start(session: str, url: str) -> None:
    """Navigate to `url` if session owns no tab OR the tab is at a no-content
    URL. Preserves existing meaningful state for state-routed dispatch.
    """
    current = session_tab_url(session)
    is_blank = current is None or current.startswith(("about:", "chrome://newtab"))

    if current is None:
        action = ["opencli", "browser", session, "tab", "new", url]
    elif is_blank:
        action = ["opencli", "browser", session, "open", url]
    else:
        print(f"[wrapper] preserving existing tab at {current[:80]}", file=sys.stderr)
        return

    r = _run(action, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(action[:5])} failed: {r.stderr[:200]}")
    print(f"[wrapper] cold-start navigated to {url}", file=sys.stderr)
    time.sleep(INITIAL_SETTLE_S)


# ─── sub-agent dispatch ───────────────────────────────────────────────────────


def run_sub(name: str, script: str, *args: str) -> tuple[str, dict | None]:
    """Spawn a sub-agent script. Return (outcome_string, latest_trace_dict)."""
    rc = subprocess.run(
        [sys.executable, script, *args], cwd=REPO,
    ).returncode
    if rc != 0:
        return f"crashed_exit_{rc}", None

    trace_dir = Path(f"/tmp/{name}")
    traces = sorted(trace_dir.glob("*.json"),
                    key=lambda p: p.stat().st_mtime, reverse=True)
    if not traces:
        return "no_trace_written", None

    trace = json.loads(traces[0].read_text())
    return trace.get("outcome", "missing_outcome_field"), trace


# ─── state → action dispatch table ────────────────────────────────────────────


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
    # at_token_revealed — token is visible on screen but Vercel renders it
    # inside a <pre> element that opencli's tree-builder treats as opaque
    # ('h:0%' marker), so DOM regex can't reach it. Use vision via
    # `claude -p` with @<path> attachment to read the literal off the
    # rendered screenshot. Pattern verified in journal slug
    # `reading-files-with-file-path-attachment-syntax-in-claude-code-...`.
    shot = Path(f"/tmp/vercel_extract_{int(time.time())}.png")
    cap = _run(["opencli", "browser", session, "screenshot", str(shot)])
    if cap.returncode != 0:
        return "screenshot_failed", None

    prompt = (
        f"@{shot} Read the literal API token visible on this screen. The "
        f"token starts with '{VERCEL_TOKEN_PREFIX}' followed by 30-60 "
        f"alphanumeric characters. It will be displayed inside a 'Token "
        f"Created' modal, typically in a code block with a copy button. "
        f"Reply with ONLY the token literal — no surrounding quotes, no "
        f"explanation, no labels. If you cannot see a token starting with "
        f"'{VERCEL_TOKEN_PREFIX}', reply with exactly 'NO_TOKEN_VISIBLE'."
    )
    r = subprocess.run(
        ["claude", "-p"], input=prompt,
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        return f"claude_err_{r.returncode}", None

    token = r.stdout.strip().strip("'\"`").strip()
    if KEY_REGEX.match(token):
        return "done", {"trace": [{"action": {"value": token}}]}

    return "no_match", None


STATE_DISPATCH = {
    "at_signup_or_login":  _dispatch_signup,
    "in_google_oauth":     _dispatch_signup_resume,
    "in_onboarding":       _dispatch_onboarding,
    "at_token_list_page":  _dispatch_create_token,
    "at_token_revealed":   _dispatch_extract_direct,
    # `blocked_external_gate` and `unknown` are terminal → handled inline
}


# ─── disposition declare (PATCH issue status; unchanged from hyperbrowser) ───


def declare(status: str) -> None:
    api_url = os.environ.get("PAPERCLIP_API_URL")
    agent_id = os.environ.get("PAPERCLIP_AGENT_ID")
    company_id = os.environ.get("PAPERCLIP_COMPANY_ID")
    if not (api_url and agent_id and company_id):
        print(f"[declare] PAPERCLIP env vars absent — skip (standalone run)",
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
        print(f"[declare] no in_progress issue assigned — skip", file=sys.stderr)
        return

    issue = issues[0]
    # Paperclip's ISSUE_STATUSES doesn't include 'failed' — map to 'blocked'.
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
                  f"status={issue_status} (wrapper={status}) → HTTP {resp.status}",
                  file=sys.stderr)
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"[declare] PATCH failed: {e}", file=sys.stderr)


# ─── main loop ────────────────────────────────────────────────────────────────


def terminal(status: str, summary: str, key: str = "") -> None:
    declare(status)
    print(json.dumps({
        "status": status,
        "tool": "vercel",
        "api_key": key,
        "summary": summary,
    }))
    sys.exit(0 if status in ("done", "blocked") else 1)


def main() -> None:
    print(f"[wrapper] state-routed dispatcher · session={SESSION} url={URL}",
          file=sys.stderr)
    print(f"[wrapper] caps · max_outer_iter={MAX_OUTER_ITER} "
          f"state_loop_guard={STATE_LOOP_GUARD}", file=sys.stderr)

    ensure_tab_for_cold_start(SESSION, URL)

    classification_trace: list[dict] = []
    consecutive_same_state = 0
    last_state = None

    for outer in range(1, MAX_OUTER_ITER + 1):
        try:
            cls = classify(SESSION, states=VERCEL_STATES)
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
                f"{consecutive_same_state} consecutive iterations without progress",
            )

        # Terminal states
        if state == "blocked_external_gate":
            terminal("blocked", f"external gate detected: {cls['evidence']}")
        if state == "unknown":
            terminal("failed",
                     f"classifier returned unknown at iter {outer}: "
                     f"{cls['evidence']}")

        # Dispatch
        dispatch = STATE_DISPATCH.get(state)
        if dispatch is None:
            terminal("failed",
                     f"no dispatch handler for state {state!r} at iter {outer}")

        outcome, trace = dispatch(SESSION, URL)
        print(f"[wrapper]   sub-agent outcome: {outcome}", file=sys.stderr)

        # Token extraction logic — handles two cases:
        # (a) The dispatched sub-agent emitted a token literal directly
        #     (extract_direct path; or create_token if it somehow read DOM).
        # (b) The dispatched sub-agent submitted the create-form and emitted
        #     a marker (TOKEN_CREATED). Run vision-based extract_direct
        #     IMMEDIATELY before Vercel's modal transitions out of the
        #     token-visible state (~60s window observed empirically).
        if state in ("at_token_list_page", "at_token_revealed") and \
                outcome == "done" and trace:
            last_action = (trace.get("trace") or [{}])[-1].get("action") or {}
            value = (last_action.get("value") or "").strip()

            # Case (a) — direct token literal
            if KEY_REGEX.match(value):
                terminal("done",
                         f"extracted via {state} dispatch at iter {outer}",
                         key=value)

            # Case (b) — create_token submitted, run vision extraction now
            print(f"[wrapper]   running immediate vision extraction "
                  f"(create_token marker={value!r})", file=sys.stderr)
            vision_outcome, vision_trace = _dispatch_extract_direct(SESSION, URL)
            print(f"[wrapper]   vision extraction outcome: {vision_outcome}",
                  file=sys.stderr)
            if vision_outcome == "done" and vision_trace:
                vision_key = (vision_trace["trace"][-1]["action"]["value"] or "").strip()
                if KEY_REGEX.match(vision_key):
                    terminal("done",
                             f"extracted via immediate vision after "
                             f"{state} dispatch at iter {outer}",
                             key=vision_key)
            # Otherwise fall through — re-classify on next iter

    # Outer max hit
    terminal("failed",
             f"max_outer_iter={MAX_OUTER_ITER} exceeded; "
             f"classification trace: "
             f"{[c['state'] for c in classification_trace]}")


if __name__ == "__main__":
    main()
