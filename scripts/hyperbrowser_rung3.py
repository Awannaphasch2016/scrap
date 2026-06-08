#!/usr/bin/env python3
"""Rung-3 Hyperbrowser agent — single `claude -p`, no chassis, no sub-agents.

Replaces hyperbrowser_signup.py (~270 lines + shared chassis + 4 sub-agents)
with a single LLM invocation that has Bash + Read tools and does the whole
chain itself: observe state → pick action → execute → repeat until extract.

Step 1 of the vertical-compression ladder from journal slug
`multi-iteration-develop-then-test-cycle-for-browser-agent-wrappers-is-a-
single-agent-pattern-...`. Tests the hypothesis: modern Claude is smart
enough to do the full login → get-api chain in one invocation, retiring
the multi-agent scaffolding.

Invoke as:
  doppler run --project accounts --config oauth -- \\
    python scripts/hyperbrowser_rung3.py
"""

from __future__ import annotations

import os
import subprocess
import sys

OAUTH_EMAIL = os.environ.get("GOOGLE_OAUTH_EMAIL", "<unset>")
SESSION = os.environ.get("HYPERBROWSER_SESSION", "hyperbrowser")
URL = os.environ.get("HYPERBROWSER_URL", "https://app.hyperbrowser.ai/signup")


PROMPT = f"""\
You are a browser agent operating Chrome via the `opencli` CLI tool.
Your job: extract a Hyperbrowser API key end-to-end. No human in the loop.

# Goal
Reach the Hyperbrowser dashboard, find the API Key section, reveal the
masked key if needed, and extract the literal `hb_*` token value.

# Final output (emit to stdout when done, then stop)
A single line of JSON:
  {{"status": "done|blocked|failed", "tool": "hyperbrowser", "api_key": "hb_...", "summary": "<one short sentence>"}}

# Identity
You log in via Google OAuth as `{OAUTH_EMAIL}`. The Google account is
already signed in to Chrome — OAuth consent screens just need a click-through.

# Substrate · opencli session `{SESSION}`
All browser actions are shell commands you execute via Bash:

  opencli browser {SESSION} --window background <subcommand> [args]

ALWAYS pass `--window background` to avoid stealing focus from the human user.

Key subcommands:
  tab new <url>             open URL in a new tab (and bind {SESSION} to it)
  tab list                  see bound tab(s) — empty list means session owns nothing
  open <url>                navigate the bound tab to URL
  state                     get DOM snapshot — URL, title, indexed interactives [0] [1] ...
  screenshot <path>         save PNG (use Read tool with @<path> to view)
  click <index>             click element by [N] index from state (path 1, AX-tree)
  click --text "<text>"     click element by visible text (path 2, no [N] needed)
  click --testid "<id>"     click element by data-testid attribute (path 2)
  fill <index> "<text>"     fill input by index with text
  fill --text "<label>" "<text>"   fill input identified by associated label text
  keys "<key>"              press keyboard key (Enter, Escape, Tab, ArrowDown, etc.)
  wait time <seconds>       wait

Use Path 1 (`click <index>`) by default — cheapest and most reliable when
elements appear in the state's [N] list. Use Path 2 (`--text` / `--testid`)
when the element is visible but missing from the [N] list (common on
React-heavy SPAs with poor accessibility tree).

# Boot sequence
1. Run: `opencli browser {SESSION} --window background tab new {URL}`
2. Wait 5 seconds (page loads in two phases — skeleton then interactive)
3. Run: `opencli browser {SESSION} --window background state`
4. Begin the observe→act loop from there.

# Iteration loop
- Run `state` to observe current page
- Reason about what you see
- Pick ONE action (one shell command)
- Execute it
- Wait briefly (1-3 seconds for clicks that navigate)
- Repeat

# Per-state guidance (compressed from prior empirical knowledge)

[at_signup_or_login] App URL with "Continue with Google" button visible
  → Click `Continue with Google` (use [N] click or --text fallback)

[in_google_oauth] URL contains accounts.google.com
  → If accountchooser shows: click the row for `{OAUTH_EMAIL}`
  → If consent screen: click Continue / Allow (usually last visible button)
  → If password prompt and no password configured: emit status=blocked, stop

[in_onboarding] Hyperbrowser onboarding modal (Welcome / personalize / Step N of M)
  → Read pw profile if available; pick role/use-case answers from common defaults:
    Role=Developer, Use Case=Building AI agents, URL=anak's personal site
  → Click Next / Get Started / Finish to advance through steps
  → On final "You're all set" modal, click Close to reveal dashboard

[at_dashboard_masked_key] Dashboard with API Key section showing masked value (***)
  → Click the masked code block, OR click the eye/Show icon next to it
  → If a modal appears with "Copy Key" but no visible literal, take a screenshot
    and use Read with @<path> to inspect what's actually visible

[at_dashboard_with_visible_key] Dashboard with hb_* visible as plain text
  → Extract the literal hb_* value from state output
  → Emit final JSON status=done with api_key set

# Caps & exit gates
- HARD CAP: 30 shell actions max. If you exceed, emit failed and stop.
- BLOCKED: captcha, paywall, account-locked, email-verification, 2FA,
  payment form → emit status=blocked with one-line summary and stop
- STUCK: same `state` URL + same interactive count for 3 consecutive
  turns without progress → emit status=failed and stop

# Notes
- The chassis is GONE. There is no Python classifier, no sub-agent dispatch,
  no STATE_DISPATCH table. YOU are the orchestrator. Use your own judgment
  to interpret state strings and pick actions.
- Run `pwd` first to confirm you're at /home/anak/dev/scrap (the working dir).
  You should be — the wrapper invoked you from there.
- You have Read available — use it with @<path> to read screenshots when
  state's text dump doesn't give you enough to act on.

Start now. Take your first action.
"""


def main() -> None:
    if OAUTH_EMAIL == "<unset>":
        sys.stderr.write(
            "[rung3] GOOGLE_OAUTH_EMAIL not set — invoke via: "
            "doppler run --project accounts --config oauth -- python "
            "scripts/hyperbrowser_rung3.py\n"
        )
        sys.exit(2)

    sys.stderr.write(
        f"[rung3] starting · session={SESSION} url={URL} oauth_email={OAUTH_EMAIL}\n"
        f"[rung3] passing prompt of {len(PROMPT)} chars to claude -p\n"
    )

    # claude -p with Bash (for opencli) and Read (for screenshots).
    cmd = ["claude", "-p", "--allowedTools", "Bash,Read"]
    proc = subprocess.run(cmd, input=PROMPT, text=True)
    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
