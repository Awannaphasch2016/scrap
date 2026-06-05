"""LLM-driven onboarding-doer agent · completes non-skippable onboarding
flows by filling required fields plausibly using Anak's pw profile.

Use case: tools whose onboarding has no Skip/Dismiss UI (e.g. Hyperbrowser's
"Let's personalize your experience" questionnaire) — must be completed
forward, not dismissed.

Mechanism: shells out to `pw profile` at startup, injects the result into
the LLM's system prompt as ground truth, then runs the standard chassis
loop. Soft questions (use case, "what brought you here") get LLM-invented
answers consistent with the profile.

Usage:
  # Chain after signup-agent (same session, resume current tab):
  python scripts/signup_agent.py https://app.hyperbrowser.ai/signup
  python scripts/onboarding_doer_agent.py

  # Standalone against a logged-in app sitting on the onboarding screen:
  python scripts/onboarding_doer_agent.py https://app.hyperbrowser.ai/

Out of scope (will abort): payment / credit card / SSN / KYC / physical
address fields. Anything that looks like fraud-risk gating.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from agent_core import AgentConfig, build_arg_parser, run_agent


GOAL = (
    "Complete this tool's onboarding flow by filling in required fields, "
    "answering questionnaires, and clicking forward through any wizard "
    "steps. Use the USER PROFILE injected in your system prompt as ground "
    "truth for personal facts. For soft questions (use case, what are you "
    "building, what brought you here), invent plausible answers consistent "
    "with the profile. Mark done when the onboarding modal/wizard is gone "
    "AND the page shows actual product surface (dashboard, API key, "
    "settings, sidebar nav). Abort if you hit a paywall, payment gate, "
    "captcha, or anything asking for sensitive info not in the profile "
    "(SSN, credit card, physical address, KYC)."
)


EXTRA_RULES = """\
Onboarding-doer rules:
  - The USER PROFILE block above is GROUND TRUTH. NEVER invent contradicting
    personal facts (name, email, github, site, what Anak builds).
  - For soft questions, invent answers CONSISTENT with the profile. Typical
    themes Anak works on: browser automation, AI agents, agent-native CLIs
    and MCPs, web scraping, multi-step pipeline orchestration, RAG.
  - For dropdowns / radios / multi-select where exact match is ambiguous,
    prefer the most technical / developer-facing option. Anak is a developer,
    not a marketer.
  - For "company" / "team size" / "role at company" fields: treat Anak as a
    solo developer. "Personal", "Just me", "1 person", "Founder/Solo dev".
  - For "primary tech stack" or language picker: Python and TypeScript are
    the safe bets (both prominent in the profile).
  - To ADVANCE the wizard: click Next / Continue / Submit / Get Started /
    Finish / Save. To DISMISS optional modals on the way: prefer Skip /
    Maybe later / X / Close (rare — only if the onboarding flow forks).
  - Done condition: page state shows NO role=dialog or modal overlay AND
    the visible content looks like product UI (an API key field, a
    "create new" button, sidebar nav, settings page, etc.).
  - Abort conditions: payment-required modal, credit-card form, captcha,
    KYC/identity-verification screen, anything asking for physical address.
"""


def get_profile_text() -> str:
    """Pull Anak's static profile via `pw profile`. Empty string on failure."""
    try:
        r = subprocess.run(
            ["pw", "profile"], capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError:
        sys.stderr.write("warn: `pw` not in PATH; proceeding with empty profile\n")
        return ""
    if r.returncode != 0:
        sys.stderr.write(
            f"warn: pw profile exited {r.returncode}; proceeding with empty profile\n"
        )
        return ""
    return r.stdout.strip()


def main() -> None:
    ap = build_arg_parser("LLM-driven onboarding-doer agent over opencli")
    args = ap.parse_args()

    profile_text = get_profile_text()
    if profile_text:
        sys.stderr.write(
            f"[profile] loaded {len(profile_text)} chars from `pw profile`\n"
        )

    extra_rules = (
        "USER PROFILE (ground truth for personal facts)\n"
        "==============================================\n"
        f"{profile_text}\n\n"
        f"{EXTRA_RULES}"
    )

    config = AgentConfig(
        name="onboarding-doer",
        goal=GOAL,
        extra_rules=extra_rules,
        # Default session matches signup-agent so the chain "just works".
        session=args.session or "signup",
        max_steps=args.max_steps,
        output_dir=Path("/tmp/onboarding_doer"),
    )
    no_nav = args.no_nav or not args.url
    run_agent(args.url, config, no_nav=no_nav)


if __name__ == "__main__":
    main()
