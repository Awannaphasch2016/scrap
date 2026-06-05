#!/usr/bin/env bash
# Wraps the proven 3-agent Hyperbrowser chain as ONE bundle for Paperclip's
# `process` adapter. YAGNI v1: URL is hardcoded. When a second tool needs
# signup, copy this script + swap the URL — that pressure will design the
# right abstraction (not speculation).
#
# Reads PAPERCLIP_* env vars for context but does not call back to the API —
# the `process` adapter captures our stdout + exit code automatically.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(dirname "$SCRIPT_DIR")"   # repo root: /home/anak/dev/scrap

URL="https://app.hyperbrowser.ai/signup"

# All three sub-agents log to stderr; final-line JSON to stdout.
# Capture get_api's last line — that's the bare API key.
python scripts/signup_agent.py "$URL"
python scripts/onboarding_doer_agent.py --no-nav
KEY=$(python scripts/get_api_agent.py --no-nav 2>/dev/null | tail -1)

# Final-line JSON Paperclip records as the run result.
printf '{"status":"done","tool":"hyperbrowser","api_key":"%s"}\n' "$KEY"
