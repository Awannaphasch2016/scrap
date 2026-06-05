"""LLM-driven signup agent · completes Google OAuth on arbitrary tools.

Thin wrapper over agent_core.run_agent. Supplies the goal sentence and
signup-specific judgment rules; the chassis owns the loop.

Usage:
  python scripts/signup_agent.py https://app.hyperbrowser.ai/signup
  python scripts/signup_agent.py --no-nav            # resume from current tab
  python scripts/signup_agent.py URL --session sup   # custom session name

Tip: start at the DIRECT signup URL, not the marketing site — links with
target=_blank open new Chrome tabs that escape the opencli session.
"""

from __future__ import annotations

from pathlib import Path

from agent_core import AgentConfig, build_arg_parser, run_agent


GOAL = (
    "Sign up to this tool using the 'Continue with Google' or 'Sign in with "
    "Google' button. The Google account is already signed in, so consent "
    "screens just need a click-through. Mark done when you reach a dashboard, "
    "onboarding screen, or signed-in homepage. Abort if you hit a paywall, "
    "captcha, or email verification step."
)


EXTRA_RULES = """\
Signup-specific rules:
  - Prefer 'Continue with Google' / 'Sign in with Google' over email signup.
  - On Google's accountchooser, the right picker is sometimes a div role=button
    rather than an <a> — try the first clickable element showing an email.
  - On Google's OAuth consent screen, Continue / Allow is usually the LAST
    visible button. If "interactive:" is < 10, the page is still booting —
    wait before clicking, don't guess at a higher index.
"""


def main() -> None:
    ap = build_arg_parser("LLM-driven signup agent over opencli")
    args = ap.parse_args()
    config = AgentConfig(
        name="signup",
        goal=GOAL,
        extra_rules=EXTRA_RULES,
        session=args.session or "signup",
        max_steps=args.max_steps,
        output_dir=Path("/tmp/signup_agent"),
    )
    # Empty URL implies resume — let the user skip --no-nav for chaining.
    no_nav = args.no_nav or not args.url
    run_agent(args.url, config, no_nav=no_nav)


if __name__ == "__main__":
    main()
