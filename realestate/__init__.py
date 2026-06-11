"""realestate.com.au daily property tracker.

Pipeline: scrape suburb search → diff against stored fingerprints → emit events →
sync to Google Sheets. v1 is local-only; Lambda + EventBridge deferred to v1.5
gated on Phase 3 acceptance (see ~/.claude/plans/setup-paperclip-for-me-swift-newell.md).
"""

__version__ = "0.1.0"
