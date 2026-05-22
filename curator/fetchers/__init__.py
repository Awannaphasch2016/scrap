"""Concrete Fetcher implementations · one module per source family.

Topic configs (curator/topics/<topic>.py) wire concrete fetchers into a
`{source_type: Fetcher}` dict that the pipeline uses to dispatch by
`Source.type`. Any topic can reuse any fetcher.

  reddit_posts  — RedditFetcher    · posts + top-level comments (news shape)
  reddit_jobs   — RedditJobsFetcher · [Hiring]-flair + seeker filter (jobs)
  hn_hiring     — HNHiringFetcher   · monthly "Who is hiring?" thread comments
  remoteok      — RemoteOKFetcher
  remotive      — RemotiveFetcher
  wwr_rss       — WWRRSSFetcher     · WeWorkRemotely category RSS
"""
