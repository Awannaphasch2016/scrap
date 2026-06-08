"""LLM-driven create-api-token agent · creates a new API token on a
logged-in dashboard and extracts the literal value shown ONCE post-creation.

Distinct from get_api_agent.py: that one assumes the key already exists in the
dashboard and just needs revealing (Hyperbrowser pattern). This one is for the
"create token → shown once → must copy immediately" pattern used by Vercel,
Cursor, OpenAI, Anthropic Console, etc.

Mechanism: standard agent_core loop. LLM is instructed to encode the extracted
token in the `done` action's `value` field. main() reads it from the trace and
prints to stdout so the token can be piped into Doppler / env / a file.

Usage:
  # Chain after signup (same session, resume current tab):
  python scripts/signup_agent.py https://vercel.com/account/settings/tokens
  python scripts/create_token_agent.py

  # Standalone against a logged-in app:
  python scripts/create_token_agent.py https://vercel.com/account/settings/tokens

Out of scope (will abort): pages requiring payment to create a token, scope
requirements that need fields we don't have (e.g. "select your team" with no
visible team), email-verification gates.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from agent_core import AgentConfig, build_arg_parser, run_agent


# Default token name embeds timestamp so multiple runs don't collide on names.
DEFAULT_TOKEN_NAME = f"paperclip-agent-{int(time.time())}"
TOKEN_NAME = os.environ.get("CREATE_TOKEN_NAME", DEFAULT_TOKEN_NAME)


GOAL = (
    f"Submit the create-token form on the current dashboard. The flow is: "
    f"find the 'Create' / 'Generate' / 'New Token' button on the tokens / API "
    f"keys / credentials page, click it (this may also be a Submit button on "
    f"a pre-rendered form), fill in a descriptive name (use exactly "
    f"'{TOKEN_NAME}'), select scope and expiration as required, then submit "
    f"the create form. "
    f"AS SOON AS the create form is submitted (after the final Create / "
    f"Generate / Submit click), emit `done` with value='TOKEN_CREATED' "
    f"WITHOUT WAITING for the post-creation modal to render. The wrapper "
    f"handles literal extraction via vision from a fresh screenshot — your "
    f"job ends at successful submission. Do NOT attempt to read the token "
    f"value from the DOM — many vendor UIs render the token inside opaque "
    f"<pre> elements that the chassis cannot index, and waiting wastes the "
    f"narrow window during which vendors expose the token. "
    f"Abort if the create flow requires payment, email verification, team "
    f"selection with no team available, or if a required form field has no "
    f"obvious default."
)


EXTRA_RULES = f"""\
Create-token rules:
  - Token names: use '{TOKEN_NAME}' EXACTLY when prompted to name the token.
    Don't add prefixes, suffixes, or modify it.
  - Button labels to click in order:
      1. "Create Token" / "Create" / "Generate" / "New Token" / "+ Token"
      2. If asked for scope: pick "Full Account" / "Full Access" / similar
         broadest-scope option. Use a dropdown click + option click for
         custom div-based dropdowns.
      3. If asked for expiration: pick the LONGEST option offered ("Never"
         / "No expiration" / "No Expiration" if shown). If only short
         options, pick the longest one.
      4. Submit button labels: "Create" / "Generate" / "Save"
  - **IMMEDIATELY after the final Create/Submit click**, emit `done` with
    value='TOKEN_CREATED'. Do NOT wait, do NOT take extra observations, do
    NOT try to read the token from any subsequent state. The wrapper does
    vision-based extraction from a fresh screenshot — your role is to
    submit the form successfully, nothing more.
  - The reason for this protocol: many vendors (Vercel observed empirically)
    show the token value for a SHORT WINDOW (~60s) inside a modal, then
    transition to a 'warning + Done button only' view. Any time spent
    interpreting the modal eats into that window. The wrapper extracts
    immediately after you finish, before the window closes.
  - If the create form has REQUIRED fields beyond name (scope, team, IP
    allowlist), pick defaults if shown. If a required field has no default
    and the choice is non-obvious, abort with the specific field name in
    the reason.
  - If you observe a form error ("Select a valid scope", "Name required",
    etc.), fix the indicated field and re-submit. Don't abort just because
    the first submit didn't accept.
  - Do NOT click "Delete" / "Revoke" / "Rotate" buttons — those destroy
    existing tokens. Only click create-flow buttons.
"""


def main() -> None:
    ap = build_arg_parser("LLM-driven create-api-token agent over opencli")
    args = ap.parse_args()

    config = AgentConfig(
        name="create-token",
        goal=GOAL,
        extra_rules=EXTRA_RULES,
        # Default session matches signup/onboarding-doer so the chain works.
        session=args.session or "signup",
        max_steps=args.max_steps,
        output_dir=Path("/tmp/create_token"),
    )
    no_nav = args.no_nav or not args.url
    result = run_agent(args.url, config, no_nav=no_nav)

    if result["outcome"] != "done":
        sys.stderr.write(
            f"[create_token] outcome={result['outcome']!r} · no token extracted\n"
        )
        sys.exit(1)

    trace = json.loads(Path(result["trace_path"]).read_text())
    last_action = trace["trace"][-1]["action"]
    token = (last_action.get("value") or "").strip()

    if not token:
        sys.stderr.write(
            "[create_token] `done` emitted but value field is empty · LLM "
            "didn't include the token. Check the trace.\n"
        )
        sys.exit(1)

    sys.stderr.write(
        f"[create_token] extracted {len(token)}-char token · "
        f"first/last 4 = {token[:4]}…{token[-4:]}\n"
    )
    # Bare token to stdout — pipes cleanly into Doppler / env / file.
    print(token)


if __name__ == "__main__":
    main()
