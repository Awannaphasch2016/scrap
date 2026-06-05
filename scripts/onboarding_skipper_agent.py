"""LLM-driven onboarding-skipper agent · clears post-signup modal noise.

Dismisses every floating modal, popup, dialog, tooltip, and sticky banner
until the page has no blocking overlay. Designed to chain after signup_agent
so the next agent (e.g. get_api_agent) lands on a clean product surface.

Stop condition: no modal-shaped element (role=dialog, aria-modal=true,
fixed-position overlay) visible in page state.

Usage:
  # Chain after signup (same session, current tab):
  python scripts/signup_agent.py https://app.hyperbrowser.ai/signup
  python scripts/onboarding_skipper_agent.py

  # Standalone against an already-logged-in app:
  python scripts/onboarding_skipper_agent.py https://app.cursor.com

  # Custom session (when signup used a non-default name):
  python scripts/onboarding_skipper_agent.py --session hb

Out of scope (will abort): required-field gates (name/company/role pickers
that don't dismiss). Different agent's job.
"""

from __future__ import annotations

from pathlib import Path

from agent_core import AgentConfig, build_arg_parser, run_agent


GOAL = (
    "Dismiss every floating modal, popup, dialog, tooltip, and sticky banner "
    "on this page until no blocking overlay remains. Mark done when the page "
    "state shows NO element with role=dialog, aria-modal=true, or any "
    "fixed-position overlay covering the viewport. Don't navigate away — "
    "stay on the current URL and just clear the noise. Abort if a modal "
    "requires filling required fields (name, company, role) to dismiss — "
    "that's a different agent's job."
)


EXTRA_RULES = """\
Onboarding-skipper rules:
  - Try `keys "Escape"` FIRST when you see any modal — it dismisses most.
  - Prefer buttons labeled "Skip", "Dismiss", "Close", "Maybe later",
    "No thanks", "X", or the close icon. AVOID "Get started", "Next",
    "Continue", "Take the tour" — those advance, they don't dismiss.
  - For cookie banners, click "Reject all" / "Decline" / "Necessary only"
    rather than "Accept all" when both are offered.
  - If a modal blocks with a REQUIRED field (cannot close without filling
    name/company/role/use-case), abort with reason
    "required field gates progress" — out of scope.
  - If you see ZERO dialog/modal/overlay elements in state and no sticky
    banners, emit done immediately. Don't second-guess.
"""


def main() -> None:
    ap = build_arg_parser("LLM-driven onboarding-skipper agent over opencli")
    args = ap.parse_args()
    config = AgentConfig(
        name="onboarding-skipper",
        goal=GOAL,
        extra_rules=EXTRA_RULES,
        # Default session = "signup" so chaining after signup_agent works
        # without flags. Override via --session for standalone use.
        session=args.session or "signup",
        max_steps=args.max_steps,
        output_dir=Path("/tmp/onboarding_skipper"),
    )
    no_nav = args.no_nav or not args.url
    run_agent(args.url, config, no_nav=no_nav)


if __name__ == "__main__":
    main()
