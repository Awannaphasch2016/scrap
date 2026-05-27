"""LLM relevance scorer · shells out to `claude -p` with a profile + rubric prompt.

Replaces the regex / SKILL_WEIGHTS scorer for the jobs topic. Lifted from
experiments/llm_scorer_poc.py and exposed as a reusable class so both the
PoC (after refactor) and lambda/rescore_handler.py can share it.

Auth: regular `claude -p` (NOT --bare) reads ~/.claude/.credentials.json,
which the caller is responsible for materializing (e.g. via
curator/core/claude_credentials.py:pull_credentials_to_home for Lambda).
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from typing import Any

from curator.core.types import Item

logger = logging.getLogger(__name__)


RUBRIC = """
Score 0 to 10 for fit:
  0-2  not a fit (wrong stack, deal-breakers like on-site)
  3-5  adjacent · could plausibly take but not obvious
  6-8  solid fit · profile matches multiple aspects
  9-10 exceptional · ideal client / role / scope

Return ONLY a JSON object, no markdown fences, no other text:
{"score": <float 0-10>, "reason": "<one sentence justifying the score>"}
""".strip()


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_claude_output(stdout: str) -> dict[str, Any]:
    """Pull a {score, reason} JSON object out of claude's stdout.

    Tolerates markdown fences and trailing whitespace. Raises ValueError
    on anything we can't recover.
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


class LLMScorer:
    """Score curator Items via `claude -p` against a fixed profile.

    Thread-safe — subprocess.run + parse_claude_output are stateless. Caller
    can pool many instances or share one across a ThreadPoolExecutor.
    """

    def __init__(self, profile: str, timeout_s: int = 120) -> None:
        self._profile = profile.strip()
        self._timeout = timeout_s

    def _build_prompt(self, item: Item) -> str:
        return (
            f"PROFILE\n=======\n{self._profile}\n\n"
            f"JOB\n===\n"
            f"Title:  {item.title or '(no title)'}\n"
            f"Author: {item.author or '(none)'}\n\n"
            f"{(item.content or '').strip()}\n\n"
            f"{RUBRIC}"
        )

    def score(self, item: Item) -> dict[str, Any]:
        """Return {"score": float 0-10, "reason": str}. Raises on failure."""
        prompt = self._build_prompt(item)
        r = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=self._timeout,
        )
        if r.returncode != 0:
            raise RuntimeError(f"claude exit {r.returncode}: {r.stderr[:200]}")
        return parse_claude_output(r.stdout)
