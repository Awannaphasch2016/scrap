#!/usr/bin/env python3
"""Inline pre-rendered SVGs into HTML diagram-host divs.

Browsers block fetch() under file:// (CORS — local pages have a null origin).
Pre-inlining makes the showcase HTML pages self-contained: open them by
double-clicking, no dev server required.

For each *.html in the target dir:
  - Find <div class="diagram-host" data-src="<rel-path>"> blocks
  - Read the referenced SVG (resolved relative to the HTML file)
  - Replace any prior inlined <svg> child with the fresh content
  - Leave the data-src attribute and other siblings (<noscript>, etc.) intact

Idempotent. Run after render-diagrams.sh.
"""

import re
import sys
from pathlib import Path

DIAGRAM_HOST_RE = re.compile(
    r'(<div class="diagram-host" data-src="([^"]+)">)(.*?)(</div>)',
    re.DOTALL
)
INNER_SVG_RE = re.compile(r'<svg[^>]*>.*?</svg>', re.DOTALL)
LEADING_XML_DECL_RE = re.compile(r'^\s*<\?xml[^?]*\?>\s*', re.DOTALL)


def inline_html(html_path: Path) -> bool:
    content = html_path.read_text(encoding="utf-8")
    any_change = False

    def replace(match: re.Match) -> str:
        nonlocal any_change
        opening, rel_src, inner, closing = match.groups()
        svg_file = (html_path.parent / rel_src).resolve()
        if not svg_file.exists():
            print(f"  warn ({html_path.name}): {rel_src} not found — skipping")
            return match.group(0)
        svg_text = svg_file.read_text(encoding="utf-8")
        svg_text = LEADING_XML_DECL_RE.sub("", svg_text).strip()
        # Drop any previously-inlined <svg>; keep other siblings (<noscript>, etc.)
        remaining = INNER_SVG_RE.sub("", inner, count=1).strip()
        new_inner = f"{svg_text}\n  {remaining}" if remaining else svg_text
        any_change = True
        return f"{opening}\n  {new_inner}\n{closing}"

    new_content = DIAGRAM_HOST_RE.sub(replace, content)
    if any_change and new_content != content:
        html_path.write_text(new_content, encoding="utf-8")
        return True
    return False


def main(target_dir: str) -> int:
    base = Path(target_dir).resolve()
    if not base.is_dir():
        print(f"not a directory: {base}", file=sys.stderr)
        return 1
    n_updated = 0
    for html in sorted(base.glob("*.html")):
        if inline_html(html):
            print(f"updated: {html.relative_to(base.parent)}")
            n_updated += 1
    print(f"\n{n_updated} HTML file(s) updated.")
    return 0


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "docs"
    sys.exit(main(target))
