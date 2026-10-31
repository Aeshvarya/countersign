"""V8: one colour source, light and dark, and the catalogue pages and images drawn from it."""

import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from countersign.edge import tokens

HEX = re.compile(r"#[0-9a-fA-F]{6}\b")
ROLES = {"bg", "panel", "ink", "ink-2", "ink-3", "line", "soft", "go", "go-bg", "ask", "ask-bg", "no", "no-bg",
         "info", "info-bg", "accent", "accent-ink", "on-status", "cat-ink"}


class TokensFileTest(unittest.TestCase):
    def tearDown(self):
        tokens.themes.cache_clear()

    def test_every_role_has_a_light_and_a_dark_colour(self):
        themes = tokens.themes()
        self.assertTrue(ROLES <= set(themes["light"]), ROLES - set(themes["light"]))
        self.assertEqual(set(themes["light"]), set(themes["dark"]))
        for theme in themes.values():
            for role, value in theme.items():
                self.assertRegex(value, r"^#[0-9a-f]{6}$", role)
        self.assertGreaterEqual(len(tokens.categories()), 1)

    def test_a_broken_file_is_refused(self):
        source = tokens.SOURCE.read_text()
        drifted = source.replace("  --bg: #0d0d0f;", "  --bg: #000000;", 1)  # only the device-dark block
        no_dark = source[:source.index("@media")]
        for text, error in ((drifted, "differ"), (no_dark, "three blocks")):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "tokens.css"
                path.write_text(text)
                tokens.themes.cache_clear()
                with mock.patch.object(tokens, "SOURCE", path), self.assertRaisesRegex(ValueError, error):
                    tokens.themes()

    def test_the_inlined_copy_is_the_file_without_its_comment(self):
        css = tokens.css()
        self.assertTrue(css.startswith(":root {"))
        self.assertIn('@media (prefers-color-scheme: dark)', css)
        self.assertIn(':root[data-theme="dark"]', css)
        self.assertTrue(tokens.block().startswith("/* tokens:begin") and tokens.block().endswith("/* tokens:end */"))
        self.assertRegex(tokens.block(), tokens.MARKED)


if __name__ == "__main__":
    unittest.main()
