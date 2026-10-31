"""V8: copy edge/static/tokens.css into every web page, between its tokens markers.

    python scripts/apply_tokens.py           rewrite any page that is out of date
    python scripts/apply_tokens.py --check   change nothing; exit 1 if a page is out of date

The pages' security policy allows inline styles only, so each page carries the
tokens itself. Run this after every change to tokens.css: a test fails until you do.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from countersign.edge import tokens

PAGES_DIR = Path(__file__).resolve().parent.parent / "src" / "countersign" / "web" / "static"


def refresh(text: str) -> str:
    """The page with its one tokens block replaced by the current file's."""
    found = tokens.MARKED.findall(text)
    if len(found) != 1:
        raise ValueError(f"expected one tokens block, found {len(found)}")
    return tokens.MARKED.sub(lambda _: tokens.block(), text)


def main(argv: list[str] | None = None, *, pages_dir: Path = PAGES_DIR, out=print) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="change nothing; exit 1 if a page is out of date")
    args = parser.parse_args(argv)
    stale = 0
    for page in sorted(pages_dir.glob("*.html")):
        text = page.read_text()
        try:
            new = refresh(text)
        except ValueError as err:
            out(f"FAIL  {page.name}: {err}")
            stale += 1
            continue
        if new == text:
            out(f"ok    {page.name}")
        elif args.check:
            out(f"STALE {page.name}: run python scripts/apply_tokens.py")
            stale += 1
        else:
            page.write_text(new)
            out(f"wrote {page.name}")
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
