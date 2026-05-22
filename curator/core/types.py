"""Topic-agnostic dataclasses shared by every curator topic.

`Source` describes one ingestion endpoint (e.g. r/WebScraping, RemoteOK).
`Item` is the unit of curated content. Both are deliberately flat — anything
topic-specific (scores, flair, salary) lives in `Item.metadata`.
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass
class Source:
    id: str
    name: str
    type: str
    url: str = ""


@dataclasses.dataclass
class Item:
    id: str
    source_id: str
    type: str  # topic-defined · "post"/"comment" for news, "job" for jobs
    title: str
    url: str
    author: str
    content: str
    published_at: float
    metadata: dict = dataclasses.field(default_factory=dict)
