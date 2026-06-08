#!/usr/bin/env python3
"""Rung-3 Cursor agent — single `claude -p`, no chassis, no sub-agents.

Replaces cursor_signup.py (~290 lines + shared chassis + 4 sub-agents) with
a single LLM invocation. Tests rung-3 viability on a style-#3 site
(React with poor a11y on the main panel; requires path-2 sidebar nav).

Differences from vercel_rung3.py:
  - Google OAuth (vs GitHub)
  - anakwannaphaschaiyong@gmail.com identity
  - Token format: crsr_* (or cursor_sk_* / key_*)
  - URL: cursor.com/dashboard → requires sidebar nav to /dashboard/api?section=user-keys
  - Path-2 click needed (URL navigation ignored by Cursor's React Router)
  - Auth on separate subdomain: authenticator.cursor.sh

Invoke as:
  doppler run --project accounts --config oauth -- \\
    python scripts/cursor_rung3.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

OAUTH_EMAIL = os.environ.get("GOOGLE_OAUTH_EMAIL", "<unset>")
SESSION = os.environ.get("CURSOR_SESSION", "cursor")
URL = os.environ.get("CURSOR_URL", "https://cursor.com/dashboard")
TOKEN_NAME = f"paperclip-rung3-{int(time.time())}"


PROMPT = f"""\
You are a browser agent operating Chrome via the `opencli` CLI tool.
Your job: create a Cursor API key and extract its literal value.

# Goal
Reach the Cursor dashboard, navigate to the API Keys subpage via the
SIDEBAR (URL navigation does not work — Cursor's React Router ignores
direct `open` calls), click Create / Generate, fill the form, and EXTRACT
the literal `crsr_*` (or `cursor_sk_*` / `key_*`) token from the reveal
modal.

# Final output (emit to stdout when done, then stop)
A single line of JSON:
  {{"status": "done|blocked|failed", "tool": "cursor", "api_key": "crsr_...", "summary": "<one short sentence>"}}

# Identity
You log in via Google OAuth as `{OAUTH_EMAIL}`. You're likely already
signed into Cursor — if you land at the dashboard directly, skip auth.

# Substrate · opencli session `{SESSION}`
All browser actions are shell commands you execute via Bash:

  opencli browser {SESSION} --window background <subcommand> [args]

ALWAYS pass `--window background` to avoid stealing focus from the human user.

Key subcommands (same as other tools):
  tab new <url>, tab list, open <url>, state, screenshot <path>
  click <index>, click --text "<text>", click --testid "<id>"
  fill <index> "<text>", fill --text "<label>" "<text>"
  keys "<key>", wait time <seconds>
  eval "<js>"  — execute JS in page context (useful for reading opaque
                 <pre> contents: e.g. document.querySelector('code').innerText)

# Boot sequence
1. `opencli browser {SESSION} --window background tab new {URL}`
2. Wait 5s for page to load
3. `opencli browser {SESSION} --window background state`
4. Begin observe→act loop.

# Per-state guidance

[at_signup_or_login] On authenticator.cursor.sh (separate subdomain!)
  → Page has Continue with Google / GitHub / Apple / Email + a Continue button
  → Click `Continue with Google` link (data-testid hint: not always present;
    use index from state — Google option is usually marked "Last used")
  → After OAuth, you'll redirect back to cursor.com

[in_google_oauth] URL contains accounts.google.com
  → If accountchooser shows: pick the row showing `{OAUTH_EMAIL}`
  → If consent screen: click Continue / Allow (last button)
  → If password prompt and no password set → emit status=blocked, stop

[at_dashboard_other] Logged in but NOT at API Keys page
  ⚠ CRITICAL: Do NOT use `open <url>` to navigate to API Keys.
  Cursor's React Router ignores it — you'll stay on /dashboard root.
  → Use path-2: `click --text "API Keys"` to click the sidebar link
  → Then wait 3s and run state again

[at_api_keys_page] URL has section=user-keys or shows "API Keys" heading
  → Page shows a Create form with a Name input + Create button
  → Fill Name with EXACTLY: `{TOKEN_NAME}`
  → Click Create
  → Modal will appear with the just-created key

[at_token_revealed] "User API Key Created" modal showing the literal value
  TIMING: Cursor's modal seems more durable than Vercel's, but extract fast.
  → Take a screenshot: `screenshot /tmp/cursor_rung3_<ts>.png`
  → Use Read with @<path> to read the literal token from the image
  → OR try `opencli browser {SESSION} eval "document.querySelector('code, pre').innerText"`
    to read the opaque <pre> contents — this often works
  → Emit final JSON status=done with the literal value

# Caps & exit gates
- HARD CAP: 30 shell actions max
- BLOCKED: captcha, paywall, "Pro plan required for API keys" gate,
  2FA challenge → emit status=blocked and stop
- STUCK: same URL + same interactive count 3 turns in a row → status=failed

# Notes
- Style #3 rendering: Cursor's main panel uses styled divs without
  accessibility tree presence on the root /dashboard. The SIDEBAR navigation
  IS chassis-friendly (semantic <a> elements) — that's why path-2
  click --text works on the sidebar but URL nav does not on the main panel.
- The chassis is GONE. You orchestrate end-to-end.

Start now. Take your first action.
"""


def main() -> None:
    if OAUTH_EMAIL == "<unset>":
        sys.stderr.write(
            "[rung3] GOOGLE_OAUTH_EMAIL not set — invoke via: "
            "doppler run --project accounts --config oauth -- python "
            "scripts/cursor_rung3.py\n"
        )
        sys.exit(2)

    sys.stderr.write(
        f"[rung3] starting · session={SESSION} url={URL} "
        f"oauth_email={OAUTH_EMAIL}\n"
        f"[rung3] token name to create: {TOKEN_NAME}\n"
        f"[rung3] passing prompt of {len(PROMPT)} chars to claude -p\n"
    )

    cmd = ["claude", "-p", "--allowedTools", "Bash,Read"]
    proc = subprocess.run(cmd, input=PROMPT, text=True)
    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
