"""Shared formatting helpers for topic renderers and bundlers.

Topic-specific HTML templates and item-row formats live in each topic module
(news_curator.Renderer / job_curator.Renderer) because the per-source row
shape, palette, and header chrome diverge enough that forcing a shared
base class produces more parameterization than reuse.

What lives here · helpers truly shared across topics:
  - `_humanize_age` — "5m ago" / "2h ago" / "3d ago" formatter for posted-at
    deltas. Same shape both topics need; clamped at 0 to avoid negative
    durations when an item's published_at is slightly in the future
    (clock skew between fetcher and renderer).
"""

from __future__ import annotations


def _humanize_age(ts: float, now_ts: float) -> str:
    delta = max(0.0, now_ts - ts)
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    return f"{int(delta / 86400)}d ago"
