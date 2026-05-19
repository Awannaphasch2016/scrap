#!/usr/bin/env bash
# Render all .puml files under diagrams/ to SVG via PlantUML + smetana layout.
# Auto-downloads plantuml.jar on first run.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JAR="$REPO_ROOT/tools/plantuml.jar"
SRC_DIR="$REPO_ROOT/diagrams"
PLANTUML_VERSION="1.2025.2"

if [[ ! -f "$JAR" ]]; then
    mkdir -p "$REPO_ROOT/tools"
    curl -fsSL -o "$JAR" \
        "https://github.com/plantuml/plantuml/releases/download/v${PLANTUML_VERSION}/plantuml-${PLANTUML_VERSION}.jar"
fi

mkdir -p "$SRC_DIR/rendered"
java -jar "$JAR" -tsvg -Playout=smetana -o rendered "$SRC_DIR"/*.puml

# Inline SVGs into showcase HTML pages so they work via file:// (no fetch).
INLINER="$REPO_ROOT/scripts/inline-svgs.py"
if [[ -f "$INLINER" && -d "$REPO_ROOT/docs" ]]; then
    python3 "$INLINER" "$REPO_ROOT/docs"
fi
