#!/usr/bin/env python3
"""Unified rung-3 agent — ONE `claude -p` invocation handles ALL 3 tools.

Step 2 of the vertical-compression ladder:
  Step 0: 3 websites × multi-agent each (chassis + sub-agents) — see *_signup.py
  Step 1: 3 websites × single claude -p each — see *_rung3.py (kept around)
  Step 2: 3 websites × single SHARED claude -p (this file) ← we are here

The unified prompt = UNIVERSAL_TEMPLATE + per-tool config slots filled in.
Three knob types:
  - simple substitutions: url, session, oauth_email, token_prefix, etc.
  - selectable presets: OAUTH_RECIPES[provider], EXTRACTION_RECIPES[pattern]
  - free-form extras: per-tool addenda for state recipes + footer notes

Adding a new tool = adding a TOOL_CONFIGS entry. Zero code edits.

Invoke as:
  doppler run --project accounts --config oauth -- \\
    python scripts/unified_rung3.py hyperbrowser
  doppler run --project accounts --config oauth -- \\
    python scripts/unified_rung3.py vercel
  doppler run --project accounts --config oauth -- \\
    python scripts/unified_rung3.py cursor
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time


# ─── selectable preset: OAuth recipe per provider ──────────────────────────────

GOOGLE_OAUTH_RECIPE = """\
[at_signup_or_login] App/auth URL with "Continue with Google" button visible
  → Click `Continue with Google` (use [N] click or --text fallback)

[in_google_oauth] URL contains accounts.google.com
  → If accountchooser shows: click the row for `{oauth_email}`
  → If consent screen: click Continue / Allow (usually last visible button)
  → If password prompt and no password configured: emit status=blocked, stop
"""

GITHUB_OAUTH_RECIPE = """\
[at_signup_or_login] App/auth URL with "Continue with GitHub" button visible
  → Click `Continue with GitHub` (data-testid=login/github-button or by text)
  → After OAuth, you should return to the target page

[in_github_oauth] URL contains github.com
  → If credentials prompt: enter `{oauth_email}` in the username/email field;
    if password prompt and no password set → emit status=blocked, stop
  → If "Authorize <app>" consent screen: click the green Authorize button
    (usually the last full-width button)
"""

OAUTH_RECIPES = {
    "google": GOOGLE_OAUTH_RECIPE,
    "github": GITHUB_OAUTH_RECIPE,
}


# ─── selectable preset: token-extraction pattern ───────────────────────────────

DASHBOARD_VISIBLE_RECIPE = """\
[at_dashboard_masked_key] Dashboard with API Key section showing masked value (***)
  → Click the masked code block, OR click the eye/Show icon next to it
  → If a modal appears with "Copy Key" but no visible literal, take a screenshot
    and use Read with @<path> to inspect what's actually visible

[at_dashboard_with_visible_key] Dashboard with {token_prefix}* visible as plain text
  → Extract the literal {token_prefix}* value from state output
  → Emit final JSON status=done with api_key set
"""

CREATE_WITH_MODAL_RECIPE = """\
[at_token_list_page / at_api_keys_page] URL with token-list view + Create button
  → Page has a form: Token Name (text input), maybe Scope + Expiration dropdowns
  → Fill Token Name with EXACTLY: `{token_name}`
  → If scope dropdown exists: pick the broadest option ("Full Account" / "Full Access")
  → If expiration dropdown: pick "No Expiration" or longest available
  → Click Create / Generate
  → IMMEDIATELY proceed to the token-revealed step — do NOT delay

[at_token_revealed] Modal showing the literal {token_prefix}* value
  TIMING-CRITICAL: some vendors hide the token after ~60 seconds. ACT FAST:
  → Take a screenshot RIGHT AWAY: `opencli browser {session} screenshot /tmp/extract_<ts>.png`
  → Use Read with @<path> to read the literal {token_prefix}* value from the image
  → OR try `opencli browser {session} eval "document.querySelector('code, pre').innerText"`
    to read opaque <pre> contents — this often works
  → Emit final JSON status=done with the extracted {token_prefix}* literal
"""

EXTRACTION_RECIPES = {
    "dashboard_visible": DASHBOARD_VISIBLE_RECIPE,
    "create_with_modal": CREATE_WITH_MODAL_RECIPE,
}


# ─── universal template ────────────────────────────────────────────────────────

UNIVERSAL_TEMPLATE = """\
You are a browser agent operating Chrome via the `opencli` CLI tool.
Your job: {goal}

# Final output (emit to stdout when done, then stop)
A single line of JSON:
  {{"status": "done|blocked|failed", "tool": "{tool_name}", "api_key": "{token_example}", "summary": "<one short sentence>"}}

# Identity
You log in via {provider_caps} OAuth as `{oauth_email}`. {identity_note}

# Substrate · opencli session `{session}`
All browser actions are shell commands you execute via Bash:

  opencli browser {session} --window background <subcommand> [args]

ALWAYS pass `--window background` to avoid stealing focus from the human user.

Key subcommands:
  tab new <url>                      open URL in a new tab (and bind {session} to it)
  tab list                           see bound tab(s) — empty list means session owns nothing
  open <url>                         navigate the bound tab to URL
  state                              get DOM snapshot — URL, title, indexed [N] interactives
  screenshot <path>                  save PNG (use Read tool with @<path> to view)
  click <index>                      click element by [N] index (path 1, AX-tree)
  click --text "<text>"              click element by visible text (path 2)
  click --testid "<id>"              click element by data-testid attribute (path 2)
  fill <index> "<text>"              fill input by [N] with text
  fill --text "<label>" "<text>"     fill input identified by associated label text
  select <index> "<option>"          pick native <select> option by visible text
  keys "<key>"                       press keyboard key (Enter, Escape, Tab, ArrowDown)
  wait time <seconds>                wait
  eval "<js>"                        execute JS in page context (useful for reading
                                     opaque <pre>: `document.querySelector('code').innerText`)

Use Path 1 (`click <index>`) by default — cheapest and most reliable when
elements appear in the state's [N] list. Use Path 2 (`--text` / `--testid`)
when the element is visible but missing from the [N] list (common on
React-heavy SPAs with poor accessibility tree).

# Boot sequence
1. Run: `opencli browser {session} --window background tab new {url}`
2. Wait 5 seconds (page loads in two phases — skeleton then interactive)
3. Run: `opencli browser {session} --window background state`
4. Begin the observe→act loop.

# Per-state guidance

{oauth_recipe}
{extraction_recipe}
{extra_recipes}

# Caps & exit gates
- HARD CAP: 30 shell actions max. If you exceed, emit failed and stop.
- BLOCKED: captcha, paywall, account-locked, email-verification, 2FA,
  payment form, WAF challenge ("Verifying your browser") → emit
  status=blocked with one-line summary and stop
- STUCK: same `state` URL + same interactive count for 3 consecutive
  turns without progress → emit status=failed and stop

# Notes
- The chassis is GONE. There is no Python classifier, no sub-agent dispatch.
  YOU are the orchestrator. Use your own judgment to interpret state strings
  and pick actions.
- You have Read available — use it with @<path> to read screenshots when
  state's text dump doesn't give you enough to act on.
{extra_notes}

Start now. Take your first action.
"""


# ─── per-tool configs ──────────────────────────────────────────────────────────

TOOL_CONFIGS = {
    "hyperbrowser": {
        "url": "https://app.hyperbrowser.ai/signup",
        "session": "hyperbrowser",
        "provider": "google",
        "oauth_email_env": "GOOGLE_OAUTH_EMAIL",
        "token_prefix": "hb_",
        "token_example": "hb_...",
        "extraction": "dashboard_visible",
        "goal": (
            "Reach the Hyperbrowser dashboard, find the API Key section, "
            "reveal the masked key if needed, and extract the literal hb_* "
            "token value."
        ),
        "identity_note": (
            "The Google account is already signed in to Chrome — OAuth "
            "consent screens just need a click-through."
        ),
        "extra_recipes": (
            "[in_onboarding] Hyperbrowser onboarding modal (Welcome / Step N of M)\n"
            "  → Pick role/use-case defaults: Role=Developer, "
            "Use Case=Building AI agents\n"
            "  → Click Next / Get Started / Finish to advance through steps\n"
            "  → On final 'You're all set' modal, click Close to reveal dashboard\n"
        ),
        "extra_notes": "",
    },
    "vercel": {
        "url": "https://vercel.com/account/settings/tokens",
        "session": "vercel",
        "provider": "github",
        "oauth_email_env": "GITHUB_OAUTH_EMAIL",
        "token_prefix": "vcp_",
        "token_example": "vcp_...",
        "extraction": "create_with_modal",
        "goal": (
            "Reach the Vercel tokens page, fill the Create-Token form, click "
            "Create, and EXTRACT the literal vcp_* token from the post-create "
            "reveal modal within ~60 seconds."
        ),
        "identity_note": (
            "You may already be signed into Vercel via GitHub — if you land "
            "at the tokens page directly, skip auth."
        ),
        "extra_recipes": "",
        "extra_notes": (
            "- For the create-token form: Vercel sometimes shows a "
            "'Select a valid scope' error if you submit without scope. Pick "
            "scope FIRST, then expiration, then submit.\n"
            "- The 'Token Created' modal has a NARROW WINDOW (~60s) showing "
            "the token literal. Extract immediately; don't waste extra reads.\n"
        ),
    },
    "cursor": {
        "url": "https://cursor.com/dashboard",
        "session": "cursor",
        "provider": "google",
        "oauth_email_env": "GOOGLE_OAUTH_EMAIL",
        "token_prefix": "crsr_",  # or cursor_sk_ / key_ for older accounts
        "token_example": "crsr_...",
        "extraction": "create_with_modal",
        "goal": (
            "Reach the Cursor dashboard, navigate to API Keys via the SIDEBAR "
            "(URL navigation FAILS — Cursor's React Router ignores `open` "
            "calls), click Create, fill the form, and extract the literal "
            "crsr_* (or cursor_sk_* / key_*) token."
        ),
        "identity_note": (
            "You're likely already signed into Cursor — if you land at the "
            "dashboard directly, skip auth."
        ),
        "extra_recipes": (
            "[at_dashboard_other] Logged in but NOT at API Keys page\n"
            "  ⚠ Do NOT use `open <url>` to navigate to API Keys.\n"
            "  Cursor's React Router ignores it — you'll stay on /dashboard root.\n"
            "  → Use path-2: `click --text \"API Keys\"` to click the sidebar link\n"
            "  → Then wait 3s and run state again\n"
        ),
        "extra_notes": (
            "- Style #3 rendering: Cursor's main panel uses styled divs without\n"
            "  accessibility-tree presence on the root /dashboard. The SIDEBAR\n"
            "  IS chassis-friendly (semantic <a> elements) — that's why path-2\n"
            "  `click --text` works on the sidebar but URL nav does not on the\n"
            "  main panel.\n"
        ),
    },
}


# ─── prompt builder ────────────────────────────────────────────────────────────


def build_prompt(tool_name: str) -> str:
    """Assemble the universal template + per-tool config + selectable presets."""
    config = TOOL_CONFIGS[tool_name]
    oauth_email = os.environ.get(config["oauth_email_env"], "<unset>")
    if oauth_email == "<unset>":
        raise RuntimeError(
            f"{config['oauth_email_env']} not set in env. Invoke via "
            f"`doppler run --project accounts --config oauth -- python "
            f"scripts/unified_rung3.py {tool_name}`"
        )

    token_name = f"paperclip-unified-{int(time.time())}"

    oauth_recipe = OAUTH_RECIPES[config["provider"]].format(
        oauth_email=oauth_email,
    )
    extraction_recipe = EXTRACTION_RECIPES[config["extraction"]].format(
        token_prefix=config["token_prefix"],
        token_name=token_name,
        session=config["session"],
    )

    return UNIVERSAL_TEMPLATE.format(
        tool_name=tool_name,
        goal=config["goal"],
        token_example=config["token_example"],
        provider_caps=config["provider"].capitalize(),
        oauth_email=oauth_email,
        identity_note=config["identity_note"],
        session=config["session"],
        url=config["url"],
        oauth_recipe=oauth_recipe,
        extraction_recipe=extraction_recipe,
        extra_recipes=config["extra_recipes"],
        extra_notes=config["extra_notes"],
    )


# ─── main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Unified rung-3 login → get-api agent — one claude -p, "
                    "any tool that's been registered in TOOL_CONFIGS.",
    )
    ap.add_argument("tool", choices=list(TOOL_CONFIGS),
                    help="Which tool to extract API key from")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the assembled prompt to stdout, don't invoke claude")
    args = ap.parse_args()

    prompt = build_prompt(args.tool)

    if args.dry_run:
        print(prompt)
        return

    sys.stderr.write(
        f"[unified rung3] tool={args.tool} "
        f"prompt_chars={len(prompt)} "
        f"oauth={TOOL_CONFIGS[args.tool]['provider']}\n"
    )

    cmd = ["claude", "-p", "--allowedTools", "Bash,Read"]
    proc = subprocess.run(cmd, input=prompt, text=True)
    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
