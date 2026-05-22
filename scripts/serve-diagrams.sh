#!/usr/bin/env bash
# Serve the repo root over HTTP so showcase pages under docs/ can fetch()
# their pre-rendered SVGs and Mermaid sources. The viewer/expander
# (diagrams/viewer/diagram-viewer.js) uses fetch() to inline SVGs into the
# DOM, which Chromium blocks from file:// origins — so this script (or any
# equivalent static HTTP server) is mandatory for the showcase pages to work.
#
# Usage:
#   ./scripts/serve-diagrams.sh
#   PORT=9000 ./scripts/serve-diagrams.sh
#   SHOWCASE_PATH=docs/c4-curator-topics.html ./scripts/serve-diagrams.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${PORT:-8000}"
SHOWCASE_PATH="${SHOWCASE_PATH:-docs/}"

echo "Serving $REPO_ROOT on http://localhost:$PORT"
echo "Showcase page: http://localhost:$PORT/$SHOWCASE_PATH"
echo "(Override with PORT=9000 SHOWCASE_PATH=docs/c4-curator-topics.html ./scripts/serve-diagrams.sh)"
echo

cd "$REPO_ROOT"
exec python3 -m http.server "$PORT"
