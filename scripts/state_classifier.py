"""LLM+screenshot state classifier for state-routed agent wrappers.

Captures a screenshot from an opencli session, hands it to `claude -p` via
the Read tool, and gets back one of N pre-defined state strings.

Path-1 design (per journal slug
`claude-p-cli-has-no-direct-image-path-flag-three-working-alternatives-...`):
the screenshot stays LOCAL — never uploaded. Privacy escalation avoided.
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


def _run(cmd: list[str], *, timeout: int = 120,
         stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, input=stdin,
    )


def capture_screenshot(session: str, out_path: Path) -> None:
    """opencli screenshot of the bound tab."""
    r = _run(["opencli", "browser", session, "screenshot", str(out_path)])
    if r.returncode != 0:
        raise RuntimeError(f"opencli screenshot failed: {r.stderr[:200]}")


def build_prompt(states: dict[str, str], image_path: Path) -> str:
    """Construct the classifier prompt. Asks Claude to use Read on the local
    image and respond with strict JSON.
    """
    state_lines = "\n".join(
        f"  - `{name}`: {desc}" for name, desc in states.items()
    )
    return f"""\
Use the Read tool to load {image_path}. Then classify the screenshot into
EXACTLY ONE of these page states:

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
             screenshot_dir: Path = Path("/tmp/state_classifier"),
             allowed_tools: str = "Read") -> dict[str, str]:
    """Capture screenshot, ask claude -p to classify, return {state, evidence,
    screenshot_path, latency_s}.
    """
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    shot = screenshot_dir / f"{session}_{ts}.png"
    capture_screenshot(session, shot)

    prompt = build_prompt(states, shot)
    start = time.time()
    r = _run(
        ["claude", "-p", "--allowedTools", allowed_tools],
        stdin=prompt,
        timeout=120,
    )
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
