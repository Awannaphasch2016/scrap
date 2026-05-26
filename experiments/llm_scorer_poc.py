"""claude -p relevance scorer · PoC · throwaway.

Reads recent jobs items from Supabase, scores each via `claude -p`, writes
score back to curator.items.relevance (+ metadata.llm_reason). The UI then
reflects the new ranking on next page load.

NOT integration · just a single script · run manually · ~80 LoC.

Env (via Doppler):
  PUBLIC_SUPABASE_URL              · for REST GETs (publishable key sufficient)
  PUBLIC_SUPABASE_PUBLISHABLE_KEY  · GET items
  SUPABASE_SECRET_KEY              · PATCH writes (service_role)

Run:
  doppler run --project scrape --config dev -- python3 experiments/llm_scorer_poc.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests

# ---- Configuration ----

WINDOW_HOURS = 36
LIMIT = 200          # high cap · we want everything in the window
WORKERS = 5          # parallel claude -p subprocesses · 5 keeps laptop comfortable
TOPIC = "jobs"
CLAUDE_TIMEOUT_S = 120

PROFILE = """
Anak is a freelance AI / automation engineer based in Bangkok.
Builds: LLM agents, web scrapers, MCP servers, marketing automation,
Telegram / Discord bots, Astro / Next.js frontends, AWS Lambda backends.
Stack: Python, TypeScript, AWS, Supabase, Pulumi. Comfortable with
Anthropic Claude API + Claude Code CLI as primary LLM tooling.
Prefers remote, contract or project-scoped work (~2-6 weeks). Refuses
on-site, US-only, full-time-employment-only postings.
""".strip()

RUBRIC = """
Score 0 to 10 for fit:
  0-2  not a fit (wrong stack, deal-breakers like on-site)
  3-5  adjacent · could plausibly take but not obvious
  6-8  solid fit · profile matches multiple aspects
  9-10 exceptional · ideal client / role / scope

Return ONLY a JSON object, no markdown fences, no other text:
{"score": <float 0-10>, "reason": "<one sentence justifying the score>"}
""".strip()

# ---- Supabase REST ----

URL = os.environ["PUBLIC_SUPABASE_URL"].rstrip("/")
PUB_KEY = os.environ["PUBLIC_SUPABASE_PUBLISHABLE_KEY"]
SEC_KEY = os.environ["SUPABASE_SECRET_KEY"]

H_READ = {
    "apikey": PUB_KEY,
    "Authorization": f"Bearer {PUB_KEY}",
    "Accept-Profile": "curator",
    "Accept": "application/json",
}
H_WRITE = {
    "apikey": SEC_KEY,
    "Authorization": f"Bearer {SEC_KEY}",
    "Content-Profile": "curator",
    "Content-Type": "application/json",
    "Prefer": "return=minimal",
}


def fetch_recent_jobs(limit: int) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)).isoformat()
    params = {
        "topic": f"eq.{TOPIC}",
        "published_at": f"gte.{cutoff}",
        "select": "id,topic,title,author,content,relevance,metadata,published_at",
        "order": "published_at.desc",
        "limit": str(limit),
    }
    r = requests.get(f"{URL}/rest/v1/items", headers=H_READ, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


# ---- Scoring ----

def build_prompt(item: dict) -> str:
    return (
        f"PROFILE\n=======\n{PROFILE}\n\n"
        f"JOB\n===\n"
        f"Title:  {item.get('title') or '(no title)'}\n"
        f"Author: {item.get('author') or '(none)'}\n\n"
        f"{(item.get('content') or '').strip()}\n\n"
        f"{RUBRIC}"
    )


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_claude_output(stdout: str) -> dict:
    """Pull a {score, reason} JSON object out of claude's stdout.

    claude -p sometimes wraps in ```json ... ``` fences or adds a trailing
    newline; tolerate both. Raise ValueError on anything we can't recover.
    """
    text = stdout.strip()
    text = _FENCE_RE.sub("", text).strip()
    obj = json.loads(text)
    if not isinstance(obj, dict) or "score" not in obj or "reason" not in obj:
        raise ValueError(f"missing keys in: {obj!r}")
    score = float(obj["score"])
    if not (0.0 <= score <= 10.0):
        raise ValueError(f"score out of range: {score}")
    return {"score": score, "reason": str(obj["reason"])[:500]}


def score_with_claude(item: dict) -> dict:
    prompt = build_prompt(item)
    r = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True, text=True, timeout=CLAUDE_TIMEOUT_S,
    )
    if r.returncode != 0:
        raise RuntimeError(f"claude exit {r.returncode}: {r.stderr[:200]}")
    return parse_claude_output(r.stdout)


# ---- Write back ----

def patch_score(item: dict, llm: dict) -> None:
    new_meta = dict(item.get("metadata") or {})
    new_meta["llm_reason"] = llm["reason"]
    new_meta["llm_scored_at"] = datetime.now(timezone.utc).isoformat()
    body = {"relevance": llm["score"], "metadata": new_meta}
    params = {"id": f"eq.{item['id']}", "topic": f"eq.{item['topic']}"}
    r = requests.patch(
        f"{URL}/rest/v1/items",
        headers=H_WRITE, params=params, data=json.dumps(body), timeout=30,
    )
    if r.status_code not in (200, 204):
        raise RuntimeError(f"PATCH failed HTTP {r.status_code}: {r.text[:200]}")


# ---- Driver ----

def score_and_patch(item: dict) -> tuple[dict, dict | None, str | None]:
    """Score one item via claude -p and PATCH back. Returns (item, llm, err)."""
    try:
        llm = score_with_claude(item)
    except Exception as e:  # noqa: BLE001
        return (item, None, f"SCORE: {e}")
    try:
        patch_score(item, llm)
    except Exception as e:  # noqa: BLE001
        return (item, llm, f"WRITE: {e}")
    return (item, llm, None)


def main() -> int:
    items = fetch_recent_jobs(LIMIT)
    t0 = datetime.now()
    print(f"fetched {len(items)} jobs items in last {WINDOW_HOURS}h · scoring with {WORKERS} parallel workers...\n")

    results: list[tuple[dict, dict | None, str | None]] = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(score_and_patch, item): item for item in items}
        for i, fut in enumerate(as_completed(futures), 1):
            item, llm, err = fut.result()
            title = (item.get("title") or "(no title)")[:60]
            if err:
                print(f"  [{i:>2}/{len(items)}] ERR    {title}\n           {err}")
            else:
                print(f"  [{i:>2}/{len(items)}] {llm['score']:>4.1f}  {title}")
            results.append((item, llm, err))

    dt = (datetime.now() - t0).total_seconds()
    ok = sum(1 for _, llm, err in results if llm and not err)
    failed = len(results) - ok
    print(f"\n=== summary · {ok}/{len(items)} scored · {failed} failed · {dt:.1f}s wall ===")
    # rank-sorted descending
    sorted_ok = sorted(
        [(it, llm) for (it, llm, err) in results if llm and not err],
        key=lambda x: -x[1]["score"],
    )
    for item, llm in sorted_ok:
        title = (item.get("title") or "(no title)")[:60]
        print(f"  {llm['score']:>4.1f}  {title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
