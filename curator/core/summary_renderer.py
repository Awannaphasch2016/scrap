"""Render today's curator items into one markdown blob for NotebookLM.

The blob becomes the single source uploaded into the ephemeral daily notebook
(see lambda/summary_handler.py). Shape matches the existing per-topic bundlers
(curator/topics/news.py, jobs.py) so NotebookLM has consistent structure to
summarize across topics.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable


CONTENT_TRUNCATE_CHARS = 1500


def _fmt_score(item: dict) -> str:
    """LLM score (relevance) if present, else regex_score from metadata, else —."""
    rel = item.get("relevance")
    if isinstance(rel, (int, float)):
        return f"LLM {rel:.1f}"
    md = item.get("metadata") or {}
    rgx = md.get("regex_score")
    if isinstance(rgx, (int, float)):
        return f"regex {rgx:.1f}"
    return "unscored"


def _fmt_item(item: dict) -> str:
    score = _fmt_score(item)
    title = item.get("title") or "(no title)"
    source = item.get("source_id") or "?"
    author = item.get("author") or "(anon)"
    published = item.get("published_at") or "?"
    url = item.get("url") or ""
    content = (item.get("content") or "").strip()
    if len(content) > CONTENT_TRUNCATE_CHARS:
        content = content[:CONTENT_TRUNCATE_CHARS] + " …[truncated]"

    lines = [
        f"### [{score}] {title}",
        f"Source: {source} · Author: {author} · Posted: {published}",
    ]
    if url:
        lines.append(f"Link: {url}")
    if content:
        lines.append("")
        lines.append(content)
    return "\n".join(lines)


def render_summary_source_md(
    items: Iterable[dict],
    now: datetime | None = None,
) -> str:
    """Build the markdown blob NotebookLM will summarize.

    Groups items by topic (jobs, scraping), heading per topic, items sorted by
    LLM relevance desc within each topic. Returns a single string suitable
    for SourcesAPI.add_text(content=...).
    """
    items = list(items)
    now = now or datetime.now(timezone.utc)
    by_topic: dict[str, list[dict]] = {}
    for it in items:
        by_topic.setdefault(it.get("topic", "unknown"), []).append(it)

    # Within each topic, rank by LLM relevance desc (None last)
    for arr in by_topic.values():
        arr.sort(
            key=lambda x: (x.get("relevance") is None, -(x.get("relevance") or 0.0)),
        )

    lines = [
        f"# Curator daily digest — {now.date().isoformat()}",
        "",
        f"Cross-topic items ingested by the daily run at "
        f"{now.strftime('%Y-%m-%d %H:%M UTC')} · {len(items)} items total",
        "",
    ]
    for topic in sorted(by_topic.keys()):
        topic_items = by_topic[topic]
        lines.append(f"## {topic} ({len(topic_items)} items)")
        lines.append("")
        for it in topic_items:
            lines.append(_fmt_item(it))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
