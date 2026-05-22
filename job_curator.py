"""Re-export shim · the real jobs topic lives at curator.topics.jobs.

This file exists so legacy import paths (lambda/job_handler.py's
`from job_curator import main`) keep resolving after the Stage 7 refactor
moved the topic config + scorer + Renderer/Bundler into curator.topics.jobs.

New code should import from `curator.topics.jobs` and `curator.core.*`
directly; this shim is scheduled to be deleted in Stage 9 once the
remaining callers are updated.
"""

from curator.topics.jobs import TOPIC, main, score_item

__all__ = ["main", "TOPIC", "score_item"]


if __name__ == "__main__":
    main()
