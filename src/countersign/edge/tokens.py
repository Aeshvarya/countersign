"""V8: the colour tokens every page and product image takes its colours from.

One file, `static/tokens.css`, holds them: light by default, dark when the
device prefers it, and `data-theme="light"` or `"dark"` on <html> forces either.

Nothing links that file. The pages' security policy allows inline styles only,
and an SVG used as an image cannot load a stylesheet. So the web pages carry a
copy of its blocks between the tokens markers (`scripts/apply_tokens.py` writes
it, a test keeps it equal), and the catalogue pages and images read it here.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

SOURCE = Path(__file__).parent / "static" / "tokens.css"
BEGIN = "/* tokens:begin, copied from src/countersign/edge/static/tokens.css by scripts/apply_tokens.py: change colours there */"
END = "/* tokens:end */"
MARKED = re.compile(r"/\* tokens:begin.*?\*/.*?/\* tokens:end \*/", re.S)

_DECL = re.compile(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\s*;")
_LIGHT = re.compile(r"^:root \{(.*?)^\}", re.S | re.M)
_DARK_MEDIA = re.compile(r'^@media \(prefers-color-scheme: dark\) \{\s*:root:not\(\[data-theme="light"\]\) \{(.*?)\}\s*\}',
                         re.S | re.M)
_DARK_FORCED = re.compile(r'^:root\[data-theme="dark"\] \{(.*?)^\}', re.S | re.M)


def css() -> str:
    """The three blocks, without the file's leading comment: what a page inlines."""
    source = SOURCE.read_text()
    return source[source.index(":root {"):].strip() + "\n"


def block() -> str:
    """The marked copy a web page carries."""
    return f"{BEGIN}\n{css()}{END}"


def _decls(body: str) -> dict[str, str]:
    return {name: value.lower() for name, value in _DECL.findall(body)}


@lru_cache(maxsize=1)
def themes() -> dict[str, dict[str, str]]:
    """{"light": {role: hex}, "dark": {role: hex}}, after checking the file is whole."""
    source = SOURCE.read_text()
    light, media, forced = _LIGHT.search(source), _DARK_MEDIA.search(source), _DARK_FORCED.search(source)
    if not (light and media and forced):
        raise ValueError("tokens.css lost one of its three blocks (light, dark by device, dark forced)")
    dark = _decls(media.group(1))
    if dark != _decls(forced.group(1)):
        raise ValueError("tokens.css: the two dark blocks differ")
    out = {"light": _decls(light.group(1)), "dark": dark}
    if set(out["light"]) != set(dark):
        raise ValueError("tokens.css: light and dark name different roles")
    return out


def categories() -> list[str]:
    """The product-image roles, cat-1 to cat-n, in order."""
    return sorted((r for r in themes()["light"] if re.fullmatch(r"cat-\d+", r)), key=lambda r: int(r[4:]))
