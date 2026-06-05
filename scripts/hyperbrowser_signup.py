#!/usr/bin/env python3
"""Paperclip process-adapter wrapper for the Hyperbrowser signup chain.

**Rung 2 — state-routed dispatcher** (per journal slug
`agent-wrapper-architecture-choice-hardcoded-python-pipeline-vs-llm-orchestrator-with-sub-agents-as-skills-...`).

Pipeline replaced with an outer loop:
  observe state → classify (LLM + screenshot) → dispatch sub-agent → repeat

Same 3 sub-agents (signup, onboarding-doer, get-api), unchanged.
Only the wrapper changed.

Defense-in-depth caps (per journal slug
`defense-in-depth-nested-termination-bounds-...`):
  - max_outer_iter = 10  (hard cap on dispatch iterations per run)
  - state_loop_guard = 3 (abort if classifier returns same state 3x in a row)
  - sub-agents have their own inner max_steps=15 from the chassis

Contract Paperclip sees (unchanged from rung-1 version):
  stdout final line: {"status": "done|blocked|failed", "tool": "hyperbrowser",
                      "api_key": "<key>", "summary": "<one-line reason>"}
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
from state_classifier import classify, HYPERBROWSER_STATES   # noqa: E402


URL = os.environ.get("HYPERBROWSER_URL", "https://app.hyperbrowser.ai/signup")
REPO = Path(__file__).parent.parent
SESSION = os.environ.get("HYPERBROWSER_SESSION", "signup")
KEY_REGEX = re.compile(r"^hb_[a-zA-Z0-9]{20,}$")

MAX_OUTER_ITER = 10
STATE_LOOP_GUARD = 3   # abort if same state classifier output N consecutive iterations
INITIAL_SETTLE_S = 5   # let new tabs render before first classification

# Substrings in an abort-reason that map to `blocked` rather than `failed`.
BLOCKED_SIGNALS = ("required", "captcha", "payment", "kyc", "verification",
                   "gate", "limit", "locked")


def _run(cmd: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess:
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
    """Navigate to `url` if the session owns no tab OR the current tab is at
    a no-content URL (about:blank, chrome://newtab, etc.). Preserves existing
    meaningful state so state-routed dispatch picks up wherever the world is.
    """
    current = session_tab_url(session)
    is_blank = current is None or current.startswith(("about:", "chrome://newtab"))

    if current is None:
        action = ["opencli", "browser", session, "tab", "new", url]
    elif is_blank:
        action = ["opencli", "browser", session, "open", url]
    else:
        # Real URL already loaded — preserve it for state-routed observation
        print(f"[wrapper] preserving existing tab at {current[:80]}", file=sys.stderr)
        return

    r = _run(action, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(action[:5])} failed: {r.stderr[:200]}")
    print(f"[wrapper] cold-start navigated to {url}", file=sys.stderr)
    time.sleep(INITIAL_SETTLE_S)


def extract_key_from_state(session: str) -> str:
    """When state classifier reports `at_dashboard_with_visible_key`, the key
    is in the DOM as plain text. Pull it with a single state read + regex.
    """
    r = _run(["opencli", "browser", session, "state"], timeout=30)
    if r.returncode != 0:
        return ""
    m = re.search(r"\bhb_[a-zA-Z0-9]{20,}\b", r.stdout)
    return m.group(0) if m else ""


# ─── sub-agent dispatch ───────────────────────────────────────────────────────


def run_sub(name: str, script: str, *args: str) -> tuple[str, dict | None]:
    """Spawn a sub-agent script. Return (outcome_string, latest_trace_dict).
    outcome_string is one of: 'done', 'abort: ...', 'max_steps_exceeded',
    'crashed_exit_<n>', 'no_trace_written'.
    """
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


# Each value is a callable: invoked with (session, url_for_cold_start) and
# returns (sub_agent_outcome_str, trace_dict_or_None). For terminal states
# (extract / abort), the callable handles them inline.

def _dispatch_signup(session: str, url: str) -> tuple[str, dict | None]:
    return run_sub("signup_agent", "scripts/signup_agent.py", url)


def _dispatch_signup_resume(session: str, url: str) -> tuple[str, dict | None]:
    # in_google_oauth — signup_agent is mid-flow; resume on the current tab
    return run_sub("signup_agent", "scripts/signup_agent.py",
                   "--no-nav", "--session", session)


def _dispatch_onboarding(session: str, url: str) -> tuple[str, dict | None]:
    return run_sub("onboarding_doer", "scripts/onboarding_doer_agent.py",
                   "--no-nav", "--session", session)


def _dispatch_get_api(session: str, url: str) -> tuple[str, dict | None]:
    return run_sub("get_api", "scripts/get_api_agent.py",
                   "--no-nav", "--session", session)


def _dispatch_extract_direct(session: str, url: str) -> tuple[str, dict | None]:
    key = extract_key_from_state(session)
    # Return a synthetic "done" outcome with the key in a fake trace shape
    # so the main loop's terminal handling can pick it up.
    return "done", {"trace": [{"action": {"value": key}}]}


STATE_DISPATCH = {
    "at_signup_or_login":            _dispatch_signup,
    "in_google_oauth":               _dispatch_signup_resume,
    "in_onboarding":                 _dispatch_onboarding,
    "at_dashboard_masked_key":       _dispatch_get_api,
    "at_dashboard_with_visible_key": _dispatch_extract_direct,
    # `blocked_external_gate` and `unknown` are terminal → handled inline in main()
}


# ─── disposition declare (PATCH issue status; unchanged from rung-1) ──────────


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
    """Declare disposition + emit final-line JSON + exit."""
    declare(status)
    print(json.dumps({
        "status": status,
        "tool": "hyperbrowser",
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
            cls = classify(SESSION, states=HYPERBROWSER_STATES)
        except Exception as e:
            print(f"[wrapper] classifier error: {e}", file=sys.stderr)
            terminal("failed", f"classifier error at iter {outer}: {e}")

        state = cls["state"]
        classification_trace.append({"iter": outer, **cls})
        print(f"[wrapper] iter {outer}/{MAX_OUTER_ITER}  state={state}  "
              f"evidence={cls['evidence'][:120]!r}", file=sys.stderr)

        # State-loop guard — same state N consecutive iterations
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
        if state == "at_dashboard_with_visible_key":
            key = extract_key_from_state(SESSION)
            if KEY_REGEX.match(key):
                terminal("done", f"extracted directly from state at iter {outer}",
                         key=key)
            else:
                terminal("failed",
                         f"visible-key state but regex failed: got {key[:8]!r}")

        # Dispatch
        dispatch = STATE_DISPATCH.get(state)
        if dispatch is None:
            terminal("failed",
                     f"no dispatch handler for state {state!r} at iter {outer}")

        outcome, trace = dispatch(SESSION, URL)
        print(f"[wrapper]   sub-agent outcome: {outcome}", file=sys.stderr)

        # If get_api just ran successfully, the key is in its trace's last action
        if state == "at_dashboard_masked_key" and outcome == "done" and trace:
            last_action = (trace.get("trace") or [{}])[-1].get("action") or {}
            key = (last_action.get("value") or "").strip()
            if KEY_REGEX.match(key):
                terminal("done", f"extracted via get_api at iter {outer}",
                         key=key)
            # Otherwise fall through — re-classify on next iter

    # Outer max hit
    terminal("failed",
             f"max_outer_iter={MAX_OUTER_ITER} exceeded; "
             f"classification trace: "
             f"{[c['state'] for c in classification_trace]}")


if __name__ == "__main__":
    main()
