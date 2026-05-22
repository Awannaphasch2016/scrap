"""Lambda entrypoint shim · forwards to the unified curator handler.

The function-level CMD is `handler.lambda_handler` (set in lambda/Dockerfile),
so this module name is load-bearing. CURATOR_TOPIC=scraping in the Lambda
environment makes the unified handler dispatch to curator.topics.news.
"""

from curator.core.handler import lambda_handler

__all__ = ["lambda_handler"]
