"""Shared substrate for topic curators (scraping, jobs, etc.).

Topic-specific code lives in `curator.topics.<topic>`; the orchestration and
infrastructure (Source/Item types, Store, Fetcher ABC, NotebookLM upload,
S3 publish, Lambda handler bootstrap) lives in `curator.core` and is
parameterized by a `TopicConfig`.
"""
