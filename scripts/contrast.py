"""V8: WCAG 2.x contrast for every pair the pages use, in light and dark, from edge/static/tokens.css.

    python scripts/contrast.py            the table; exit 0
    python scripts/contrast.py --strict   exit 1 if any pair fails

Text needs 4.5:1. Large text (the 120 px initials on a product image) and UI
parts that must be seen (borders, status colours on a card) need 3:1.
"""

from __future__ import annotations

import argparse
import sys

from countersign.edge import tokens

TEXT, LARGE = 4.5, 3.0

# (foreground role, background role, minimum, where it is used)
PAIRS = [
    ("ink", "bg", TEXT, "body text"),
    ("ink", "panel", TEXT, "body text on a card"),
    ("ink-2", "bg", TEXT, "secondary text"),
    ("ink-2", "panel", TEXT, "secondary text on a card"),
    ("ink-3", "panel", TEXT, "hints and labels on a card"),
    ("bg", "ink", TEXT, "dark button text (console)"),
    ("accent", "panel", TEXT, "links"),
    ("accent-ink", "accent", TEXT, "primary button text"),
    ("go", "go-bg", TEXT, "charged badge"),
    ("ask", "ask-bg", TEXT, "asked-again badge"),
    ("no", "no-bg", TEXT, "refused badge"),
    ("info", "info-bg", TEXT, "neutral note"),
    ("on-status", "ask", TEXT, "ask button text (console)"),
    ("on-status", "no", TEXT, "no button text (console)"),
    ("go", "panel", LARGE, "status colour on a card"),
    ("ask", "panel", LARGE, "status colour on a card"),
    ("no", "panel", LARGE, "status colour on a card"),
    ("line", "panel", LARGE, "borders a control needs"),
]


def luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    channels = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def ratio(a: str, b: str) -> float:
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def pairs() -> list[tuple[str, str, float, str]]:
    cats = [(cat_ink, cat, LARGE, "initials on a product image")
            for cat_ink, cat in (("cat-ink", c) for c in tokens.categories())]
    return PAIRS + cats


def rows(theme: dict[str, str]) -> list[tuple[str, str, float, float, str]]:
    return [(fg, bg, ratio(theme[fg], theme[bg]), need, where) for fg, bg, need, where in pairs()]


def main(argv: list[str] | None = None, out=print) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--strict", action="store_true", help="exit 1 if any pair fails")
    args = parser.parse_args(argv)
    failed = 0
    for name, theme in tokens.themes().items():
        out(f"{name}:")
        for fg, bg, r, need, where in rows(theme):
            ok = r >= need
            failed += not ok
            out(f"  {'PASS' if ok else 'FAIL'}  {fg:>10} on {bg:<8} {theme[fg]} on {theme[bg]}  "
                f"{r:5.2f}:1  needs {need}:1  {where}")
    out(f"{failed} pair(s) below WCAG AA")
    return 1 if args.strict and failed else 0


if __name__ == "__main__":
    sys.exit(main())
