"""LLM+screenshot state classifier for state-routed agent wrappers.

Captures a screenshot from an opencli session, hands it to `claude -p` via
the `@<path>` attachment syntax, and gets back one of N pre-defined state
strings.

Privacy: screenshot stays LOCAL — never uploaded. Per journal slug
`claude-p-cli-has-no-direct-image-path-flag-three-working-alternatives-...`.

Attachment mechanism: per journal slug
`reading-files-with-file-path-attachment-syntax-in-claude-code-...`.
Eagerly attaches the image to the model's first turn (no Read tool round-trip).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path


# Canonical 7-state vocabulary for the Hyperbrowser chain.
# Per journal slug `map-the-full-ui-state-graph-...` — the minimal classifier
# set is the compression of the full UI state graph.
HYPERBROWSER_STATES = {
    "at_signup_or_login":
        "App URL with 'Continue with Google' / 'Sign in with Google' button. "
        "Could be /signup or /login (look similar). Dispatch: signup_agent.",
    "in_google_oauth":
        "URL contains accounts.google.com. Account chooser, consent screen, "
        "or password prompt. Dispatch: signup_agent (continues across these).",
    "in_onboarding":
        "App URL with a modal (role=dialog) covering most of the page. "
        "Modal mentions 'Welcome', 'personalize', 'Step N of M', or has a "
        "'Get Started' / 'Finish' button. Dispatch: onboarding_doer_agent.",
    "at_dashboard_masked_key":
        "App dashboard (no modal). 'API Key' heading visible. Key is shown "
        "as asterisks or bullets (e.g., ***********). Dispatch: get_api_agent.",
    "at_dashboard_with_visible_key":
        "App dashboard. Key shown as plain text matching hb_[a-z0-9]+. "
        "Dispatch: extract directly (no sub-agent needed).",
    "blocked_external_gate":
        "Captcha, paywall, account-locked banner, email-verification required, "
        "2FA challenge, or payment form. Dispatch: declare blocked + abort.",
    "unknown":
        "None of the above match. Page doesn't resemble any expected state. "
        "Dispatch: declare failed + abort with screenshot for diagnosis.",
}


# Vocabulary for Vercel — create-token flow instead of dashboard-visible key.
# Key differences from HYPERBROWSER_STATES:
#   - No 'masked_key' state (Vercel doesn't show keys on dashboard).
#   - Adds 'at_token_list_page' (where the Create button lives).
#   - Adds 'at_token_revealed' (post-create reveal page; goal-reached).
#   - 'in_onboarding' covers Vercel's team-creation / plan-picker wizard.
# Vocabulary for Cursor. Auth lives on authenticator.cursor.sh (separate
# subdomain from cursor.com). Tokens are created from the dashboard's API
# Keys subpage and shown only once (similar pattern to Vercel).
CURSOR_STATES = {
    "at_signup_or_login":
        "URL on authenticator.cursor.sh OR cursor.com/sign-in. 'Continue "
        "with Google' / 'Continue with GitHub' / 'Continue with Apple' "
        "buttons visible alongside an email field. Dispatch: signup_agent.",
    "in_google_oauth":
        "URL contains accounts.google.com. Account chooser, consent screen, "
        "or password prompt. Dispatch: signup_agent (continues across these).",
    "in_onboarding":
        "Post-login welcome/onboarding wizard on cursor.com. URL may contain "
        "/onboarding or /welcome. Shows 'Get Started' / 'Choose Plan' / "
        "'Install Cursor' tile cards or similar setup flow. "
        "Dispatch: onboarding_doer_agent.",
    "at_api_keys_page":
        "URL on cursor.com/dashboard with 'API Keys' heading visible (or "
        "/dashboard/api-keys). Page shows 'Create API Key' / 'New Key' / "
        "'Generate Key' button, possibly with an existing-keys table. "
        "Dispatch: create_token_agent.",
    "at_token_revealed":
        "Modal or page showing a just-created API key as plain text. Key "
        "likely starts with `cursor_sk_*`, `key_*`, or similar prefix; "
        "warning that key won't be shown again, with a Copy button. "
        "Dispatch: extract directly via vision.",
    "at_dashboard_other":
        "Logged-in cursor.com surface that is NOT the API Keys page (e.g. "
        "/agents, /dashboard root with sidebar visible but no API Keys "
        "heading). Need to navigate via sidebar to API Keys subpage. "
        "Dispatch: navigate_to_api_keys (inline).",
    "blocked_external_gate":
        "Captcha, paywall, account-locked banner, email-verification "
        "required, 2FA challenge, payment form, or 'You need a Pro plan to "
        "use API keys' gate. Dispatch: declare blocked + abort.",
    "unknown":
        "None of the above match. Page doesn't resemble any expected state. "
        "Dispatch: declare failed + abort with screenshot for diagnosis.",
}


VERCEL_STATES = {
    "at_signup_or_login":
        "URL is vercel.com/signup or vercel.com/login. 'Continue with Google' "
        "button visible (alongside GitHub, GitLab, Apple options). "
        "Dispatch: signup_agent.",
    "in_google_oauth":
        "URL contains accounts.google.com. Account chooser, consent screen, "
        "or password prompt. Dispatch: signup_agent (continues across these).",
    "in_onboarding":
        "Vercel post-login wizard. URL contains /new or /onboarding or shows "
        "'Create Team' / 'Choose Plan' / 'Hobby / Pro / Enterprise' tiers. "
        "Dispatch: onboarding_doer_agent.",
    "at_token_list_page":
        "URL contains /account/settings/tokens (or /account/tokens). Page "
        "shows 'Create Token' button (or 'Create' / 'New Token' / '+ Token'). "
        "May show a table of existing tokens. Dispatch: create_token_agent.",
    "at_token_revealed":
        "Just-created token visible as plain text (24-64 chars alphanumeric, "
        "may have vendor prefix). Often shown in a code block with 'Copy' "
        "button and one-time-show warning. Dispatch: extract directly.",
    "blocked_external_gate":
        "Captcha, paywall, account-locked banner, email-verification required, "
        "2FA challenge, or payment form. Dispatch: declare blocked + abort.",
    "unknown":
        "None of the above match. Page doesn't resemble any expected state. "
        "Dispatch: declare failed + abort with screenshot for diagnosis.",
}


def _run(cmd: list[str], *, timeout: int = 120,
         stdin: str | None = None) -> subprocess.CompletedProcess:
    # See agent_core._run — keep classifier screenshots from stealing focus.
    if cmd[:2] == ["opencli", "browser"] and "--window" not in cmd:
        cmd = cmd[:3] + ["--window", "background"] + cmd[3:]
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, input=stdin,
    )


def capture_screenshot(session: str, out_path: Path) -> None:
    """opencli screenshot of the bound tab."""
    r = _run(["opencli", "browser", session, "screenshot", str(out_path)])
    if r.returncode != 0:
        raise RuntimeError(f"opencli screenshot failed: {r.stderr[:200]}")


def build_prompt(states: dict[str, str], image_path: Path) -> str:
    """Construct the classifier prompt. Uses `@<path>` attachment syntax to
    eagerly attach the screenshot to the message — model sees the image
    on turn 1 without invoking the Read tool.

    Verification: see journal slug
    `reading-files-with-file-path-attachment-syntax-in-claude-code-...`
    (verified working in `-p` mode via both stdin and positional).
    """
    state_lines = "\n".join(
        f"  - `{name}`: {desc}" for name, desc in states.items()
    )
    return f"""\
@{image_path} Classify the attached screenshot into EXACTLY ONE of these page states:

{state_lines}

Respond with ONLY a JSON object — no markdown fences, no prose outside JSON:

  {{"state": "<one of the names above>",
    "evidence": "<one short sentence of what you saw that picked this state>"}}

Do not pick `unknown` if any of the other states plausibly fits. Only use
`unknown` for genuinely unrecognizable screens. Trim whitespace from the JSON.
"""


# Tolerate optional ```json fences around the LLM output.
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def classify(session: str, *,
             states: dict[str, str] = HYPERBROWSER_STATES,
             screenshot_dir: Path = Path("/tmp/state_classifier")
             ) -> dict[str, str]:
    """Capture screenshot, ask claude -p to classify, return {state, evidence,
    screenshot_path, latency_s}.
    """
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    shot = screenshot_dir / f"{session}_{ts}.png"
    capture_screenshot(session, shot)

    prompt = build_prompt(states, shot)
    start = time.time()
    r = _run(["claude", "-p"], stdin=prompt, timeout=120)
    latency = time.time() - start

    if r.returncode != 0:
        raise RuntimeError(f"claude -p exit {r.returncode}: {r.stderr[:300]}")

    text = _FENCE_RE.sub("", r.stdout.strip()).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"claude returned non-JSON: {text[:300]!r}") from e

    state = obj.get("state")
    if state not in states:
        raise ValueError(
            f"claude returned unknown state {state!r} (valid: {list(states)})"
        )

    return {
        "state": state,
        "evidence": str(obj.get("evidence", ""))[:300],
        "screenshot_path": str(shot),
        "latency_s": round(latency, 2),
    }


def main() -> None:
    """CLI entry point — for ad-hoc testing of the classifier."""
    import argparse
    ap = argparse.ArgumentParser(description="LLM+screenshot state classifier")
    ap.add_argument("--session", default="signup",
                    help="opencli session name (default: signup)")
    args = ap.parse_args()

    result = classify(args.session)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
