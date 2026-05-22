"""Lambda entrypoint shim · forwards to the unified curator handler.

The scrape-job-curator function's image-config CMD is
`job_handler.lambda_handler`, so this module name is load-bearing.
CURATOR_TOPIC=jobs in the Lambda environment makes the unified handler
dispatch to curator.topics.jobs.
"""

from curator.core.handler import lambda_handler

__all__ = ["lambda_handler"]
