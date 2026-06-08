"""LLM-driven signup agent · completes OAuth (Google or GitHub) on arbitrary
tools.

Thin wrapper over agent_core.run_agent. Supplies the goal sentence and
signup-specific judgment rules; the chassis owns the loop.

Provider selection via env var (set by the wrapper before spawning):
  OAUTH_PROVIDER=google   (default) — uses 'Continue with Google' button,
                                       Google account chooser, etc.
  OAUTH_PROVIDER=github             — uses 'Continue with GitHub' button,
                                       GitHub login page, Authorize consent.

Identity disambiguation via env var (set by doppler run --project accounts
--config oauth before spawning):
  GOOGLE_OAUTH_EMAIL — which Google account to pick at chooser
  GITHUB_OAUTH_EMAIL — which GitHub account to log in as
  GITHUB_OAUTH_PASSWORD — used if GitHub session is not active

Usage:
  python scripts/signup_agent.py https://app.hyperbrowser.ai/signup
  python scripts/signup_agent.py --no-nav            # resume from current tab
  python scripts/signup_agent.py URL --session sup   # custom session name

Tip: start at the DIRECT signup URL, not the marketing site — links with
target=_blank open new Chrome tabs that escape the opencli session.
"""

from __future__ import annotations

import os
from pathlib import Path

from agent_core import AgentConfig, build_arg_parser, run_agent


PROVIDER_INFO = {
    "google": {
        "button_text": "Continue with Google",
        "signin_text":  "Sign in with Google",
        "email_env":    "GOOGLE_OAUTH_EMAIL",
        "password_env": "GOOGLE_OAUTH_PASSWORD",
        "oauth_domain": "accounts.google.com",
        "provider_name": "Google",
    },
    "github": {
        "button_text": "Continue with GitHub",
        "signin_text":  "Sign in with GitHub",
        "email_env":    "GITHUB_OAUTH_EMAIL",
        "password_env": "GITHUB_OAUTH_PASSWORD",
        "oauth_domain": "github.com",
        "provider_name": "GitHub",
    },
}


def _resolve_provider() -> tuple[str, dict, str, str]:
    """Read OAUTH_PROVIDER + identity env vars. Returns (provider_key, info,
    email, password). Email/password may be empty strings if not set.
    """
    provider = os.environ.get("OAUTH_PROVIDER", "google").lower()
    if provider not in PROVIDER_INFO:
        raise ValueError(
            f"OAUTH_PROVIDER={provider!r} unsupported; expected one of "
            f"{list(PROVIDER_INFO)}"
        )
    info = PROVIDER_INFO[provider]
    email = os.environ.get(info["email_env"], "")
    password = os.environ.get(info["password_env"], "")
    return provider, info, email, password


def _make_goal(info: dict, email: str) -> str:
    provider = info["provider_name"]
    email_hint = (
        f"Use the {provider} account with email {email}. "
        if email else
        f"Use whichever {provider} account is currently signed in. "
    )
    return (
        f"Sign up to this tool using the '{info['button_text']}' or "
        f"'{info['signin_text']}' button. {email_hint}"
        f"If shown an account chooser or account-picker, select the row "
        f"matching that email. The account session is likely already active "
        f"in Chrome, so consent screens just need a click-through. Mark "
        f"`done` when you reach a dashboard, onboarding screen, settings "
        f"page, or any signed-in homepage. Abort if you hit a paywall, "
        f"captcha, payment form, email verification gate, or 2FA challenge."
    )


def _make_extra_rules(provider_key: str, info: dict, email: str, password: str) -> str:
    base = (
        f"Signup-specific rules:\n"
        f"  - Prefer '{info['button_text']}' / '{info['signin_text']}' over "
        f"email signup or other OAuth providers.\n"
    )
    if provider_key == "google":
        return base + f"""\
  - Identity to use: email '{email or '<unspecified — use signed-in account>'}'.
    On Google's accountchooser, click the row showing that exact email.
    If multiple rows look similar, prefer the one whose visible text matches
    the email exactly.
  - On Google's accountchooser, the right picker is sometimes a div role=button
    rather than an <a> — try the first clickable element showing the target
    email.
  - On Google's OAuth consent screen, Continue / Allow is usually the LAST
    visible button. If "interactive:" is < 10, the page is still booting —
    wait before clicking, don't guess at a higher index.
  - If a password prompt appears and {info['password_env']} is empty, abort
    with reason 'fresh-login required but no password configured in
    accounts/oauth'. Do NOT type guesses.
"""
    if provider_key == "github":
        pwd_hint = (
            f"If a password prompt appears, type {info['password_env']}'s "
            f"value into the password input. Only do this if "
            f"{info['password_env']} is non-empty; otherwise abort with reason "
            f"'fresh-login required but no password configured in "
            f"accounts/oauth'."
            if password else
            f"If a password prompt appears, abort with reason "
            f"'fresh-login required but no GITHUB_OAUTH_PASSWORD configured "
            f"in accounts/oauth' — do NOT type guesses."
        )
        return base + f"""\
  - Identity to use: GitHub login '{email or '<unspecified>'}'.
  - GitHub OAuth flow steps:
      1. Tool's signup/login page → click '{info['button_text']}' button.
      2. Redirect to github.com/login → if you see a username/email input,
         type '{email}' (use that exact value) and click Sign in.
      3. {pwd_hint}
      4. After successful login, GitHub shows an 'Authorize <app>' consent
         page. Click the green 'Authorize <app>' button (usually the LAST
         visible button on the page, full-width).
      5. After authorization, you should be redirected back to the tool's
         dashboard or signed-in surface. Mark `done` there.
  - If GitHub asks for a 2FA code (TOTP, SMS, security key), abort with
    reason '2FA challenge — TOTP seed not configured'.
  - If GitHub shows 'Reauthorization required' or any consent screen with
    new permission scopes, accept the new scopes (click Authorize) — the
    tool can't proceed otherwise.
"""
    return base


def main() -> None:
    ap = build_arg_parser("LLM-driven OAuth signup agent over opencli")
    args = ap.parse_args()

    provider_key, info, email, password = _resolve_provider()
    goal = _make_goal(info, email)
    extra_rules = _make_extra_rules(provider_key, info, email, password)

    config = AgentConfig(
        name="signup",
        goal=goal,
        extra_rules=extra_rules,
        session=args.session or "signup",
        max_steps=args.max_steps,
        output_dir=Path("/tmp/signup_agent"),
    )
    no_nav = args.no_nav or not args.url
    run_agent(args.url, config, no_nav=no_nav)


if __name__ == "__main__":
    main()
