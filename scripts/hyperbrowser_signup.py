#!/usr/bin/env python3
"""Paperclip process-adapter wrapper for the proven 3-agent Hyperbrowser chain.

Replaces the earlier 5-line bash version. The added logic (sub-agent outcome
classification + key validation + disposition PATCH back to Paperclip) pushed
the wrapper past bash's comfort zone — see journal slug
`paperclips-process-adapter-is-language-agnostic-bash-isnt-recommended-just-
convenient-at-small-wrapper-sizes`.

Contract Paperclip sees:
  stdout final line: {"status": "done|blocked|failed", "tool": "hyperbrowser",
                      "api_key": "<key>", "summary": "<one-line reason>"}
  exit code:         0 for done/blocked, 1 for failed
  PATCH callback:    /api/issues/<PAPERCLIP_TASK_ID> {status: "done|blocked|failed"}
                     (closes the load-bearing disposition gap; without it runs
                      land in recovery-blocked dead-ends — see slug
                      `paperclip-distinguishes-process-exit-from-work-disposition...`)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

URL = "https://app.hyperbrowser.ai/signup"
REPO = Path(__file__).parent.parent           # /home/anak/dev/scrap
KEY_REGEX = re.compile(r"^hb_[a-zA-Z0-9]{20,}$")

# Substrings in an `abort:<reason>` outcome that map to `blocked` rather than `failed`.
# (Recovery-blocked dispositions mean "external action could unstick this";
#  failed means "needs code/site fix.")
BLOCKED_SIGNALS = ("required", "captcha", "payment", "kyc", "verification")


def run_sub(name: str, *args: str) -> tuple[str, dict | None]:
    """Run a sub-agent CLI. Return (outcome_string, latest_trace_dict_or_None).

    `outcome_string` is one of:
      - "done"                     (sub-agent emitted done normally)
      - "abort: <reason>"          (sub-agent gave up cleanly)
      - "max_steps_exceeded"       (chassis hit hard cap)
      - "crashed_exit_<code>"      (sub-agent exited non-zero)
      - "no_trace_written"         (sub-agent ran but didn't create trace JSON)
    """
    rc = subprocess.run([sys.executable, *args], cwd=REPO).returncode
    if rc != 0:
        return f"crashed_exit_{rc}", None

    trace_dir = Path(f"/tmp/{name}")
    traces = sorted(trace_dir.glob("*.json"),
                    key=lambda p: p.stat().st_mtime, reverse=True)
    if not traces:
        return "no_trace_written", None

    trace = json.loads(traces[0].read_text())
    return trace.get("outcome", "missing_outcome_field"), trace


def classify(signup: tuple[str, dict | None],
             onboard: tuple[str, dict | None],
             getapi: tuple[str, dict | None],
             key: str) -> tuple[str, str]:
    """Map sub-agent outcomes + key validation → (disposition, summary)."""
    s, o, g = signup[0], onboard[0], getapi[0]
    combined = f"{s} | {o} | {g}"

    if any("crashed" in x for x in (s, o, g)):
        return "failed", f"sub-agent crashed: {combined}"
    if any("max_steps" in x for x in (s, o, g)):
        return "failed", f"sub-agent exhausted max_steps: {combined}"
    if any("no_trace" in x for x in (s, o, g)):
        return "failed", f"sub-agent ran but wrote no trace: {combined}"

    # Blocked vs failed for abort-with-reason cases
    aborts = [x for x in (s, o, g) if x.startswith("abort")]
    if aborts:
        if any(sig in a.lower() for a in aborts for sig in BLOCKED_SIGNALS):
            return "blocked", f"external gate detected: {combined}"
        return "failed", f"sub-agent aborted: {combined}"

    # All three say done — final validation gate
    if not KEY_REGEX.match(key):
        return "failed", (f"key validation failed: got '{key[:8]}…' "
                          f"length={len(key)}, expected ^hb_[a-zA-Z0-9]{{20,}}$")

    return "done", f"chain completed; key extracted (len={len(key)})"


def declare(status: str) -> None:
    """PATCH issue status to Paperclip. No-op if env vars absent (standalone run).

    Paperclip's process adapter only injects PAPERCLIP_AGENT_ID, _COMPANY_ID,
    _API_URL — NOT _TASK_ID. So we look up our own in_progress issue via the
    inbox query and PATCH whichever issue is currently assigned. Assumes
    exactly-one in_progress issue per agent (true for our hardcoded single-task
    wrapper; would need refinement if the agent ever multitasks).

    No auth headers needed in local_trusted deployment mode (verified via
    `/api/health`'s `"deploymentMode":"local_trusted"`).
    """
    api_url = os.environ.get("PAPERCLIP_API_URL")
    agent_id = os.environ.get("PAPERCLIP_AGENT_ID")
    company_id = os.environ.get("PAPERCLIP_COMPANY_ID")
    if not (api_url and agent_id and company_id):
        print(f"[declare] PAPERCLIP_API_URL/AGENT_ID/COMPANY_ID absent — "
              f"skipping disposition PATCH (standalone run?)", file=sys.stderr)
        return

    headers = {"Content-Type": "application/json"}
    if api_key := os.environ.get("PAPERCLIP_API_KEY"):
        headers["Authorization"] = f"Bearer {api_key}"
    if run_id := os.environ.get("PAPERCLIP_RUN_ID"):
        headers["X-Paperclip-Run-Id"] = run_id

    # 1. Look up our currently in_progress issue
    list_url = (
        f"{api_url}/api/companies/{company_id}/issues"
        f"?assigneeAgentId={agent_id}&status=in_progress"
    )
    try:
        with urllib.request.urlopen(list_url, timeout=10) as resp:
            issues = json.loads(resp.read().decode())
        issues = issues if isinstance(issues, list) else issues.get("items", [])
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"[declare] couldn't fetch assigned issues: {e}", file=sys.stderr)
        return

    if not issues:
        print(f"[declare] no in_progress issue assigned to {agent_id} — "
              f"nothing to disposition", file=sys.stderr)
        return
    if len(issues) > 1:
        ids = [it.get("identifier") for it in issues]
        print(f"[declare] {len(issues)} in_progress issues assigned ({ids}); "
              f"using first", file=sys.stderr)

    issue = issues[0]
    issue_id = issue["id"]

    # Paperclip's ISSUE_STATUSES enum is [backlog, todo, in_progress, in_review,
    # done, blocked, cancelled]. Our wrapper-internal classification uses
    # `failed` for "agent crashed / max_steps / key validation broke" — those
    # are run-level concepts, not issue-level. Map them to `blocked` (the
    # issue-level analog: needs human/external action to resolve).
    issue_status = "blocked" if status == "failed" else status

    req = urllib.request.Request(
        f"{api_url}/api/issues/{issue_id}",
        method="PATCH",
        data=json.dumps({"status": issue_status}).encode(),
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"[declare] PATCH /api/issues/{issue.get('identifier', issue_id)} "
                  f"status={issue_status} (wrapper={status}) → HTTP {resp.status}",
                  file=sys.stderr)
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"[declare] PATCH failed: {e} — issue may stay in_progress",
              file=sys.stderr)


def main() -> None:
    signup = run_sub("signup_agent",
                     "scripts/signup_agent.py", URL)
    onboard = run_sub("onboarding_doer",
                      "scripts/onboarding_doer_agent.py", "--no-nav")
    getapi = run_sub("get_api",
                     "scripts/get_api_agent.py", "--no-nav")

    # The key is in get_api's trace at trace[-1].action.value (per the
    # prompt-as-API contract documented in get_api_agent.py)
    key = ""
    if getapi[1]:
        last_action = (getapi[1].get("trace") or [{}])[-1].get("action") or {}
        key = (last_action.get("value") or "").strip()

    status, summary = classify(signup, onboard, getapi, key)
    declare(status)

    print(json.dumps({
        "status": status,
        "tool": "hyperbrowser",
        "api_key": key,
        "summary": summary,
    }))
    sys.exit(0 if status in ("done", "blocked") else 1)


if __name__ == "__main__":
    main()
