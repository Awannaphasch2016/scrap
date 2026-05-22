"""Topic configurations — one module per curator topic.

Each topic module:
  - declares its constants (DB_PATH, USER_AGENT, SOURCES, ...)
  - defines its topic-specific Renderer + Bundler (and FeedPublisher etc.)
  - builds a `TOPIC = TopicConfig(...)` and exposes `main(): run(TOPIC)`

To add a new topic, drop a new module here and a CURATOR_TOPIC-aware
Lambda handler entry (stage 8 work, currently skipped).
"""
