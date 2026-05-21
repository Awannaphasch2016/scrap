"""Python port of curator's ~/dev/curator/src/lib/feed-prompt.mjs.

Two sides of the same prompt + parser:
  - Laptop (Node)  → curator queries NotebookLM on-demand via this same prompt
  - Lambda (Python · this file) → publishes today's feed to S3 after upload

The two MUST produce comparable results · keep the prompt and item-bullet
regex in sync. If we ever diverge, the laptop-side feed (deprecated path)
and Lambda-published feed (canonical) would show different top items.
"""

import re

FEED_PROMPT = """List the 5 most notable, newest, or important items from the current digest source attached to this notebook.

For each item, give:
- A short title
- A one-line summary (concise — under 25 words)
- The direct URL to the originating Reddit post

Output as a markdown bulleted list with this EXACT format per item, one item per line:

- **<title>** — <one-line summary>. ([source](<full URL>))

CRITICAL constraints:
- The URL MUST be a real URL present in the digest source (typically https://www.reddit.com/r/.../comments/... ). Do not invent, paraphrase, or guess URLs.
- If fewer than 5 items have clearly-cited URLs in the source, return only the ones that do. Better to return 3 well-cited items than 5 with hallucinated links.
- Do not add any preamble, summary, or commentary outside the bulleted list itself.
- Do not number the items. Use the markdown bullet character "-" only."""


_ITEM_RE = re.compile(
    r"^[-*]\s+\*\*(.+?)\*\*\s*[—\-:]\s*(.+?)\s*\(\[source\]\((https?://[^\s)]+)\)\)\s*$"
)
_VALID_URL_RE = re.compile(r"^https?://(?:www\.)?(?:reddit\.com|redd\.it)\b", re.IGNORECASE)


def parse_feed_response(text: str) -> list[dict]:
    """Parse NotebookLM's bullet list into [{title, summary, url}, ...].

    Items without valid reddit URLs are silently dropped (no hallucinated
    links survive). Returns [] if nothing parses cleanly.
    """
    if not text or not isinstance(text, str):
        return []
    items: list[dict] = []
    for line in text.splitlines():
        trimmed = line.strip()
        if not (trimmed.startswith("-") or trimmed.startswith("*")):
            continue
        m = _ITEM_RE.match(trimmed)
        if not m:
            continue
        title, summary, url = m.group(1), m.group(2), m.group(3)
        if not _VALID_URL_RE.match(url):
            continue
        items.append({"title": title.strip(), "summary": summary.strip(), "url": url.strip()})
    return items


def render_item(item: dict) -> str:
    """One bullet line · symmetric with the parser."""
    return f"- **{item['title']}** — {item['summary']} ([source]({item['url']}))"
