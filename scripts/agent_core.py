"""Shared chassis for LLM-driven browser agents over opencli.

Per-agent scripts (signup_agent.py, onboarding_skipper_agent.py, …) supply:
  - name        · used for trace dir + default session name
  - goal        · what success looks like (free-text sentence for the LLM)
  - extra_rules · agent-specific judgment, appended to BASE_PROMPT

The chassis owns the universal loop: doctor gate → bootstrap tab → state →
LLM action picker → execute → repeat → final trace + screenshot.

Imports: scripts/*.py are runnable as `python scripts/<x>.py`; Python adds
scripts/ to sys.path so `from agent_core import …` works directly.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


# ---- universal system prompt --------------------------------------------------


BASE_PROMPT = """\
You drive a Chrome tab via a CLI tool. On each turn you see the current page
state (URL, title, interactive elements indexed [0], [1], ...) plus your
recent actions. You pick ONE next action.

Respond with ONLY a JSON object — no markdown fences, no prose outside JSON:

  {"thought": "<one short sentence>",
   "action":  one of the action verbs listed below,
   "target":  <int index, int pixel-x, or string identifier — see per-action semantics>,
   "value":   <string for fill text / selector / pixel-y / reason — see per-action semantics>}

Action semantics:

  Index-based (path 1 — works when the element appears in 'interactive: N'
  list with a [number] prefix):
    click          target=<index>,  value=null
    fill           target=<index>,  value="<text to type>"
    select         target=<index>,  value="<option text or value to pick>"

  DOM-based (path 2 — works when the element is visually present and you
  can identify it by text content or test-id, even if it has NO [N] index
  in the state list because it's a styled <div> without role/aria-label):
    click_text     target=null,     value="<visible text on the element>"
    click_testid   target=null,     value="<data-testid value>"
    fill_text      target="<label or visible text near the input>",  value="<text to fill>"
    fill_testid    target="<data-testid value>",                     value="<text to fill>"

  Pixel-coordinate (path 3 — last resort; use only when paths 1 and 2 both
  fail. Estimate coordinates from your visual judgment of the screenshot
  the classifier captured. Use sparingly; brittle if page reflows):
    click_xy       target=<integer x>,  value="<integer y>"

  Keyboard / waiting / control:
    keys           target=null,     value="<key name, e.g. Escape, Enter, Tab>"
    wait_time      target=null,     value="<seconds, e.g. '3'>"
    wait_selector  target=null,     value="<css selector to wait for>"
    done           target=null,     value=null            (goal reached)
    abort          target=null,     value="<reason>"

Which path to pick:
  - DEFAULT to `click` / `fill` / `select` (path 1) when the target has a
    [N] index in the interactive list. Cheapest and most reliable.
  - REACH for `click_text` / `click_testid` / `fill_text` / `fill_testid`
    (path 2) when:
      * 'interactive: 0' or very few indexed elements, but the page
        VISUALLY renders content (style #3 — React with styled-div onClick
        and no role= / aria-label =, common in some modern dashboards).
      * The target you want has visible text or a data-testid but doesn't
        appear in the [N] list.
    `click_text` is the go-to. `click_testid` is more stable but requires
    the page to use data-testid attributes.
  - REACH for `click_xy` (path 3) only when paths 1 and 2 fail — e.g.,
    canvas-rendered content (Figma, Sheets) where no DOM element exists
    for the target. Pass integer pixel coordinates relative to the
    rendered viewport.

Note on dropdowns:
  - Native <select> tags: use `select` action.
  - Custom div-styled dropdowns (most React/Tailwind UIs): use `click` to
    open the dropdown, then `click` (or `click_text`) the option from the
    next state.

Universal rules:
  - NEVER emit an index higher than the count in the "interactive: N"
    footer. If your target isn't visible AND you can't identify it by
    text/testid, emit wait_time "3" — don't guess.
  - After a click that triggers navigation, the page often renders in two
    phases: skeleton DOM first, then real interactive elements. If the
    "interactive:" count is small (< 10), the page is probably still
    booting. Emit wait_time "3" before the next click.
  - Indices re-number every turn — only use indices from the LATEST state.
  - If the same URL + interactive count repeats for 3 turns without
    progress, abort with a reason.
  - When falling back from path 1 → path 2, mention what you tried in
    `thought` so the trace records the escalation.
"""


# ---- config -------------------------------------------------------------------


@dataclass
class AgentConfig:
    name: str
    goal: str
    extra_rules: str = ""
    session: str = ""
    max_steps: int = 15
    initial_settle_s: float = 5.0
    post_click_sleep_s: float = 5.0
    output_dir: Path = field(default_factory=lambda: Path("/tmp/opencli_agents"))

    def __post_init__(self) -> None:
        if not self.session:
            self.session = self.name


# ---- opencli helpers ----------------------------------------------------------


def _run(cmd: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess:
    # `opencli browser` calls default to `--window foreground`, which fires
    # Page.bringToFront on every interaction and steals OS-level focus from
    # whatever the user is doing. Background mode keeps the tab driveable
    # without yanking the Chrome window to the front.
    if cmd[:2] == ["opencli", "browser"] and "--window" not in cmd:
        cmd = cmd[:3] + ["--window", "background"] + cmd[3:]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _strip_opencli_noise(stdout: str) -> str:
    # opencli prints an "Update available" footer on every call — drop it so
    # the LLM prompt isn't polluted with version-bump nags.
    cut = stdout.find("\n  Update available:")
    return stdout[:cut].rstrip() if cut != -1 else stdout.rstrip()


def doctor_ok() -> bool:
    r = _run(["opencli", "doctor"])
    return r.returncode == 0 and "[OK] Connectivity" in r.stdout


def session_tabs(session: str) -> list[dict]:
    r = _run(["opencli", "browser", session, "tab", "list"])
    if r.returncode != 0:
        return []
    try:
        return json.loads(_strip_opencli_noise(r.stdout))
    except json.JSONDecodeError:
        return []


def ensure_tab(session: str, url: str) -> None:
    """Idempotent: spawn an owned tab for this session if none, then navigate."""
    if not session_tabs(session):
        r = _run(["opencli", "browser", session, "tab", "new", url])
        if r.returncode != 0:
            raise RuntimeError(f"tab new failed: {r.stderr[:200]}")
        return
    r = _run(["opencli", "browser", session, "open", url])
    if r.returncode != 0:
        raise RuntimeError(f"open failed: {r.stderr[:200]}")


def get_state(session: str) -> str:
    # First call after navigation can block on DOM readiness; allow 60s.
    r = _run(["opencli", "browser", session, "state"], timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"state failed: {r.stderr[:200]}")
    return _strip_opencli_noise(r.stdout)


def screenshot(session: str, path: Path) -> None:
    _run(["opencli", "browser", session, "screenshot", str(path)])


# ---- LLM action picker --------------------------------------------------------


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_VALID_ACTIONS = {
    # Path 1 — index-based (AX-tree-friendly sites, style #1/#2/#10)
    "click", "fill", "select",
    # Path 2 — DOM-based via opencli native flags (style #3 — bare-div React,
    # opencli supports --text / --testid / --role natively on click and fill)
    "click_text", "click_testid",
    "fill_text", "fill_testid",
    # Path 3 — pixel coordinates via eval (style #4/#5 — canvas, hung
    # Suspense, or fully-opaque DOM)
    "click_xy",
    # Keyboard / waiting / control
    "keys", "wait_time", "wait_selector", "done", "abort",
}


def ask_llm(system_prompt: str, goal: str, state: str,
            history: list[dict]) -> dict:
    history_text = "\n".join(
        f"  {i+1}. {h['action']} target={h.get('target')} value={h.get('value')!r}"
        f"  // {(h.get('thought') or '')[:80]}"
        for i, h in enumerate(history[-5:])
    ) or "  (no prior steps)"

    prompt = (
        f"{system_prompt}\n\n"
        f"GOAL\n====\n{goal}\n\n"
        f"RECENT ACTIONS (most recent last)\n==================================\n"
        f"{history_text}\n\n"
        f"CURRENT PAGE STATE\n==================\n{state}\n\n"
        f"What is the next action?"
    )
    r = _run(["claude", "-p", prompt], timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"claude exit {r.returncode}: {r.stderr[:200]}")

    text = _FENCE_RE.sub("", r.stdout.strip()).strip()
    obj = json.loads(text)
    if obj.get("action") not in _VALID_ACTIONS:
        raise ValueError(f"unknown action: {obj!r}")
    return obj


# ---- action executor ----------------------------------------------------------


def execute(session: str, act: dict) -> str:
    a = act["action"]
    target = act.get("target")
    value = act.get("value") or ""

    # Path 1 — index-based
    if a == "click":
        r = _run(["opencli", "browser", session, "click", str(target)])
    elif a == "fill":
        r = _run(["opencli", "browser", session, "fill",
                  str(target), value])
    elif a == "select":
        r = _run(["opencli", "browser", session, "select",
                  str(target), value])

    # Path 2 — DOM-based via opencli native flags
    elif a == "click_text":
        r = _run(["opencli", "browser", session, "click",
                  "--text", value])
    elif a == "click_testid":
        r = _run(["opencli", "browser", session, "click",
                  "--testid", value])
    elif a == "fill_text":
        # `--text` flag picks the input by accessible-name/label text in
        # `target`; the value to fill is in `value`.
        r = _run(["opencli", "browser", session, "fill",
                  "--text", str(target), value])
    elif a == "fill_testid":
        r = _run(["opencli", "browser", session, "fill",
                  "--testid", str(target), value])

    # Path 3 — pixel coordinates via eval
    elif a == "click_xy":
        try:
            x = int(target)
            y = int(value)
        except (TypeError, ValueError):
            return (f"ERROR click_xy needs integer coords: "
                    f"target={target!r} value={value!r}")
        js = f"document.elementFromPoint({x}, {y}).click()"
        r = _run(["opencli", "browser", session, "eval", js])

    # Keyboard / waiting
    elif a == "keys":
        r = _run(["opencli", "browser", session, "keys",
                  value or "Escape"])
    elif a == "wait_time":
        r = _run(["opencli", "browser", session, "wait", "time",
                  str(value or "2")])
    elif a == "wait_selector":
        r = _run(["opencli", "browser", session, "wait", "selector",
                  value or "body"])

    else:
        return f"(noop for action={a})"

    if r.returncode != 0:
        return f"ERROR exit={r.returncode}: {r.stderr[:200]}"
    return _strip_opencli_noise(r.stdout)[:400]


# ---- main loop ----------------------------------------------------------------


def run_agent(url: str, config: AgentConfig, *, no_nav: bool = False) -> dict:
    if not doctor_ok():
        sys.stderr.write(
            "opencli doctor failed · is Chrome open with the extension connected?\n"
            "  run: opencli doctor\n"
        )
        sys.exit(2)

    config.output_dir.mkdir(parents=True, exist_ok=True)
    system_prompt = BASE_PROMPT + (
        "\n" + config.extra_rules if config.extra_rules else ""
    )

    if no_nav:
        sys.stderr.write(
            f"[resume] agent={config.name} session={config.session} (no nav)\n"
        )
    else:
        sys.stderr.write(
            f"[bootstrap] agent={config.name} session={config.session} url={url}\n"
        )
        ensure_tab(config.session, url)
        time.sleep(config.initial_settle_s)

    trace: list[dict] = []
    history: list[dict] = []
    outcome = "max_steps_exceeded"

    for step in range(1, config.max_steps + 1):
        state = get_state(config.session)
        head = (state.splitlines() or [""])[0][:120]
        sys.stderr.write(f"\n[step {step}/{config.max_steps}] {head}\n")

        try:
            act = ask_llm(system_prompt, config.goal, state, history)
        except Exception as e:
            sys.stderr.write(f"  LLM error: {e}\n")
            outcome = f"llm_error: {e}"
            break

        sys.stderr.write(f"  thought: {(act.get('thought') or '')[:120]}\n")
        sys.stderr.write(
            f"  action:  {act['action']} "
            f"target={act.get('target')} value={act.get('value')!r}\n"
        )

        history.append(act)
        result = (
            execute(config.session, act)
            if act["action"] not in {"done", "abort"}
            else ""
        )
        trace.append({
            "step": step,
            "state": state,
            "action": act,
            "result": result,
        })

        if act["action"] == "done":
            outcome = "done"
            break
        if act["action"] == "abort":
            outcome = f"abort: {act.get('value', '')}"
            break

        # Clicks often trigger cross-origin navigation (OAuth, route change) —
        # give the new page time to commit AND render its interactive layer.
        time.sleep(
            config.post_click_sleep_s if act["action"] == "click" else 1
        )

    ts = int(datetime.now().timestamp())
    trace_path = config.output_dir / f"{config.session}_{ts}.json"
    trace_path.write_text(json.dumps({
        "agent": config.name,
        "url": url,
        "goal": config.goal,
        "session": config.session,
        "outcome": outcome,
        "steps": len(trace),
        "trace": trace,
    }, indent=2))

    shot_path = config.output_dir / f"{config.session}_{ts}.png"
    screenshot(config.session, shot_path)

    sys.stderr.write(
        f"\n[done] outcome={outcome} steps={len(trace)}\n"
        f"       trace      → {trace_path}\n"
        f"       screenshot → {shot_path}\n"
    )
    return {
        "outcome": outcome,
        "steps": len(trace),
        "trace_path": str(trace_path),
    }


# ---- shared CLI surface -------------------------------------------------------


def build_arg_parser(description: str) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("url", nargs="?", default="",
                    help="Target URL (omit when using --no-nav or chaining)")
    ap.add_argument("--session", default="",
                    help="opencli session name (defaults to agent name)")
    ap.add_argument("--max-steps", type=int, default=15,
                    help="Hard cap on actions before giving up")
    ap.add_argument("--no-nav", action="store_true",
                    help="Skip bootstrap · resume from session's current tab")
    return ap
