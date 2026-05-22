"""Re-export shim · the real news (scraping) topic lives at curator.topics.news.

This file exists so legacy import paths (lambda/handler.py's
`from news_curator import main` and any external scripts doing
`from news_curator import NotebookLMUploader`) keep resolving after the
Stage 7 refactor moved the topic config + Renderer/Bundler/FeedPublisher
into curator.topics.news.

New code should import from `curator.topics.news` and `curator.core.*`
directly; this shim is scheduled to be deleted in Stage 9 once the
remaining callers are updated.
"""

from curator.core.notebooklm import NotebookLMUploader
from curator.topics.news import TOPIC, main

__all__ = ["main", "TOPIC", "NotebookLMUploader"]


if __name__ == "__main__":
    main()
