#!/usr/bin/env python3
"""Rung-3 Vercel agent — single `claude -p`, no chassis, no sub-agents.

Replaces vercel_signup.py (~270 lines + shared chassis + 4 sub-agents) with
a single LLM invocation that has Bash + Read tools and does the whole chain
itself.

Differences from hyperbrowser_rung3.py:
  - GitHub OAuth (vs Google)
  - awannaphasch2016@fau.edu identity (vs anak@gmail.com)
  - Token format: vcp_* (vs hb_*)
  - Extraction pattern: create-with-modal (vs dashboard-visible)
  - ~60s reveal window — agent must extract IMMEDIATELY after creation
  - Vision extraction recommended (Vercel renders token in opaque <pre>)

Invoke as:
  doppler run --project accounts --config oauth -- \\
    python scripts/vercel_rung3.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

OAUTH_EMAIL = os.environ.get("GITHUB_OAUTH_EMAIL", "<unset>")
SESSION = os.environ.get("VERCEL_SESSION", "vercel")
URL = os.environ.get("VERCEL_URL", "https://vercel.com/account/settings/tokens")
TOKEN_NAME = f"paperclip-rung3-{int(time.time())}"


PROMPT = f"""\
You are a browser agent operating Chrome via the `opencli` CLI tool.
Your job: create a Vercel API token and extract its literal value.

# Goal
Reach the Vercel tokens page, fill the Create-Token form, click Create,
and EXTRACT the literal `vcp_*` token from the post-create reveal modal
WITHIN A NARROW WINDOW (~60s — see "timing-critical" below).

# Final output (emit to stdout when done, then stop)
A single line of JSON:
  {{"status": "done|blocked|failed", "tool": "vercel", "api_key": "vcp_...", "summary": "<one short sentence>"}}

# Identity
You log in via GitHub OAuth as `{OAUTH_EMAIL}`. You may already be signed
into Vercel via GitHub — if you land at the tokens page directly, skip auth.

# Substrate · opencli session `{SESSION}`
All browser actions are shell commands you execute via Bash:

  opencli browser {SESSION} --window background <subcommand> [args]

ALWAYS pass `--window background` to avoid stealing focus from the human user.

Key subcommands:
  tab new <url>                      open URL in new tab (and bind session)
  tab list                           see bound tab(s)
  open <url>                         navigate bound tab to URL
  state                              get DOM snapshot — URL, title, [N] interactives
  screenshot <path>                  save PNG (use Read with @<path> to view)
  click <index>                      click by [N] index (path 1)
  click --text "<text>"              click by visible text (path 2)
  click --testid "<id>"              click by data-testid (path 2)
  fill <index> "<text>"              fill input by [N] with text
  fill --text "<label>" "<text>"     fill by associated label
  select <index> "<option>"          pick native <select> option by visible text
  keys "<key>"                       press Enter/Escape/Tab/etc.
  wait time <seconds>                wait

# Boot sequence
1. `opencli browser {SESSION} --window background tab new {URL}`
2. Wait 5s
3. `opencli browser {SESSION} --window background state`
4. Begin observe→act loop.

# Per-state guidance

[at_signup_or_login] On vercel.com/login OR vercel.com/signup
  → Click `Continue with GitHub` (data-testid=login/github-button or by text)
  → After OAuth, you should return to the tokens page

[in_github_oauth] URL contains github.com
  → If credentials prompt: enter `{OAUTH_EMAIL}` in the username/email field;
    if password prompt and no password set → emit status=blocked, stop
  → If "Authorize Vercel" consent screen: click the green Authorize button
    (usually the last full-width button)

[at_token_list_page] URL contains /account/settings/tokens with a Create form
  → The page has a form: Token Name (text input), Scope (dropdown),
    Expiration (dropdown). Plus a Create button.
  → Fill Token Name with EXACTLY: `{TOKEN_NAME}`
  → Open Scope dropdown, pick "Full Account" (the broadest option)
  → For Expiration, pick "No Expiration" (longest option)
  → Click Create button
  → IMMEDIATELY proceed to the token-revealed step — do NOT delay

[at_token_revealed] "Token Created" modal appears with the literal vcp_*
  TIMING-CRITICAL: Vercel hides the token after ~60 seconds. ACT FAST:
  → Take a screenshot RIGHT AWAY: `screenshot /tmp/vercel_rung3_<ts>.png`
  → Use Read with @<path> to read the literal vcp_* value from the image
  → The token is in a code block / <pre> element near a Copy button
  → opencli `state` may NOT expose the token (Vercel renders inside opaque
    <pre>) — vision via Read is the reliable extraction
  → Emit final JSON status=done with the extracted vcp_* literal

# Caps & exit gates
- HARD CAP: 30 shell actions max
- BLOCKED: captcha, paywall, 2FA, payment form, WAF challenge
  (e.g., "Verifying your browser") → emit status=blocked and stop
- STUCK: same URL + same interactive count 3 turns in a row → status=failed

# Notes
- The chassis is GONE. You orchestrate end-to-end.
- For the create-token form: Vercel sometimes shows a "Select a valid scope"
  error if you submit without scope. Pick scope FIRST, then expiration,
  then submit.
- After clicking Create, the next state will be the "Token Created" modal —
  go to vision extraction immediately. Do NOT do additional state reads
  that waste the 60s window.

Start now. Take your first action.
"""


def main() -> None:
    if OAUTH_EMAIL == "<unset>":
        sys.stderr.write(
            "[rung3] GITHUB_OAUTH_EMAIL not set — invoke via: "
            "doppler run --project accounts --config oauth -- python "
            "scripts/vercel_rung3.py\n"
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
