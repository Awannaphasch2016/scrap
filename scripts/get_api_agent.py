"""LLM-driven get-api-key agent · extracts a tool's API key from a
logged-in dashboard.

Designed to chain after onboarding_doer_agent — the tab should already
be at a clean product surface where the API key is reachable.

Mechanism: standard agent_core loop, but the LLM is instructed to
encode the extracted API key in the `done` action's `value` field.
main() then reads it from the trace and prints to stdout so the key
can be piped into Doppler / env / a file.

Usage:
  # Chain after onboarding_doer (same session, resume current tab):
  python scripts/onboarding_doer_agent.py https://app.hyperbrowser.ai/
  python scripts/get_api_agent.py

  # Standalone against a logged-in app:
  python scripts/get_api_agent.py https://app.hyperbrowser.ai/

  # Pipe into Doppler:
  python scripts/get_api_agent.py 2>/dev/null | \\
    doppler secrets set HYPERBROWSER_API_KEY --plain

Out of scope (will abort): payment-gated keys, copy-to-clipboard-only
keys not readable via DOM, sites that require email confirmation
before exposing the key.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from agent_core import AgentConfig, build_arg_parser, run_agent


GOAL = (
    "Find this tool's API key on the current dashboard and extract its "
    "literal value as a string. API keys are usually MASKED (shown as "
    "********* or ***...***). Find the reveal mechanism (eye icon, 'Show' "
    "button, clicking the masked code block, a 'Reveal' menu item) and "
    "click it to expose the real value. The key may live on a Settings, "
    "API Keys, Tokens, or Credentials subpage you need to navigate to via "
    "the sidebar first. When you can see the unmasked key as plain text in "
    "the page state, emit `done` with value=<the literal API key string, "
    "no surrounding labels or whitespace>. Abort if the dashboard requires "
    "further onboarding, payment to unlock the key, or if the key is only "
    "available via clipboard copy with no DOM-readable representation."
)


EXTRA_RULES = """\
Get-api rules:
  - API keys look like 32–64 chars of letters/digits, often with a vendor
    prefix: `sk-`, `hk-`, `pk_live_`, `hb-`, `pat_`, etc.
  - Locations to check, in order of likelihood:
      1. Current page (often a "Your API Key" section on the Overview/dashboard)
      2. Sidebar > Settings > API Keys / Tokens / Credentials subpage
      3. Account / Profile menu > API or Developer settings
  - Reveal patterns to try (in order):
      1. Click the masked code element itself (`<code>****</code>`)
      2. Click an adjacent eye/show icon button
      3. Click a "Reveal" or "Show" button
      4. Click a menu (3-dots, gear) near the key to find a Reveal option
  - When the key appears as PLAIN TEXT in state (no asterisks, no stars),
    emit `done` with value="<the literal key>". Trim whitespace; don't
    include `<code>`, `API Key:`, or any other surrounding markup/labels.
  - If only a "Copy" button exists (clicks-to-clipboard, key stays masked
    in DOM), try other reveal paths first. As a last resort, abort with
    reason "key is copy-to-clipboard only, not readable via DOM".
  - Reading the value out of a regular dashboard view is success — you do
    NOT need to create a new API key. Don't click "Create new key" or
    "Generate" buttons that would rotate the credential.
"""


def main() -> None:
    ap = build_arg_parser("LLM-driven get-api-key agent over opencli")
    args = ap.parse_args()

    config = AgentConfig(
        name="get-api",
        goal=GOAL,
        extra_rules=EXTRA_RULES,
        # Default session matches signup/onboarding-doer so the chain works.
        session=args.session or "signup",
        max_steps=args.max_steps,
        output_dir=Path("/tmp/get_api"),
    )
    no_nav = args.no_nav or not args.url
    result = run_agent(args.url, config, no_nav=no_nav)

    if result["outcome"] != "done":
        sys.stderr.write(
            f"[get_api] outcome={result['outcome']!r} · no key extracted\n"
        )
        sys.exit(1)

    trace = json.loads(Path(result["trace_path"]).read_text())
    last_action = trace["trace"][-1]["action"]
    api_key = (last_action.get("value") or "").strip()

    if not api_key:
        sys.stderr.write(
            "[get_api] `done` emitted but value field is empty · LLM "
            "didn't include the key. Check the trace.\n"
        )
        sys.exit(1)

    sys.stderr.write(
        f"[get_api] extracted {len(api_key)}-char key · "
        f"first/last 4 = {api_key[:4]}…{api_key[-4:]}\n"
    )
    # Bare key to stdout — pipes cleanly into Doppler / env / file.
    print(api_key)


if __name__ == "__main__":
    main()
