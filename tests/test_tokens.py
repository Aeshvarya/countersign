"""V8: one colour source, light and dark, and the catalogue pages and images drawn from it."""

import importlib.util
import re
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from countersign import receipt, tap
from countersign.edge import pages, tokens
from countersign.fx import FakeFx
from countersign.merchant import FakeMerchant, synthetic_catalog

try:
    from fastapi.testclient import TestClient

    from countersign.edge import create_app
except ImportError:  # the server extra is not installed
    TestClient = None

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "contrast.py"
_spec = importlib.util.spec_from_file_location("contrast", _PATH)
contrast = importlib.util.module_from_spec(_spec)
sys.modules["contrast"] = contrast
_spec.loader.exec_module(contrast)

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
        media, forced = source.index("@media"), source.index(':root[data-theme="dark"]')
        bg = re.search(r"--bg: (#[0-9a-fA-F]{6});", source[media:forced]).group(1)
        other = "#010203" if bg.lower() != "#010203" else "#030201"
        # only the device-dark block changes, whatever the palette is
        drifted = source[:media] + source[media:forced].replace(f"--bg: {bg};", f"--bg: {other};", 1) + source[forced:]
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


class CataloguePagesTest(unittest.TestCase):
    def setUp(self):
        self.product = synthetic_catalog()[0]
        self.light, self.dark = tokens.themes()["light"], tokens.themes()["dark"]

    def test_the_product_page_carries_both_themes_and_no_colour_of_its_own(self):
        html = pages.product_page(self.product)
        self.assertIn(tokens.css(), html)
        self.assertIn('<meta name="color-scheme" content="light dark">', html)
        self.assertIn("background: var(--bg)", html)
        self.assertEqual(HEX.findall(html.replace(tokens.css(), "")), [])

    def test_the_image_draws_light_and_switches_to_dark(self):
        svg = pages.product_svg(self.product)
        cat = pages._category(self.product)
        dark_part, _, light_part = svg.partition("</style>")  # the dark rule comes first, in <style>
        self.assertIn(f'class="bg" width="600" height="600" fill="{self.light["bg"]}"', light_part)
        self.assertIn(f'fill="{self.light[cat]}"', light_part)
        self.assertIn(f".bg {{ fill: {self.dark['bg']} }}", dark_part)
        self.assertIn(f".card {{ fill: {self.dark[cat]} }}", dark_part)
        used = {c.lower() for c in HEX.findall(svg)}
        self.assertTrue(used <= set(self.light.values()) | set(self.dark.values()), used)

    def test_categories_spread_over_the_category_colours(self):
        roles = {pages._category(p) for p in synthetic_catalog()}
        self.assertTrue(roles <= set(tokens.categories()))
        self.assertGreater(len(roles), 1)


@unittest.skipIf(TestClient is None, "needs the server extra")
class ServedTest(unittest.TestCase):
    def test_the_image_route_serves_the_token_colours(self):
        merchant = FakeMerchant(FakeFx("84.10"))
        key = Ed25519PrivateKey.generate()
        app = create_app(merchant, tap.StaticDirectory({("https://agents.example", "k1"): key.public_key()}),
                         authority="testserver", clock=lambda: datetime(2026, 11, 2, 10, tzinfo=timezone.utc),
                         signer=receipt.MerchantSigner(Ed25519PrivateKey.generate(), "m-1"))
        client = TestClient(app)
        sku = next(iter(merchant.catalog))
        svg = client.get(f"/img/{sku}.svg").text
        self.assertIn(tokens.themes()["light"]["bg"], svg)
        self.assertIn("prefers-color-scheme: dark", svg)
        self.assertIn(tokens.css(), client.get(f"/p/{sku}").text)


class ContrastTest(unittest.TestCase):
    def test_the_ratio_matches_wcag_reference_values(self):
        self.assertAlmostEqual(contrast.ratio("#000000", "#ffffff"), 21.0, places=2)
        self.assertAlmostEqual(contrast.ratio("#777777", "#ffffff"), 4.48, places=2)
        self.assertEqual(contrast.ratio("#1f3df0", "#ffffff"), contrast.ratio("#ffffff", "#1f3df0"))

    def test_every_pair_it_checks_names_a_role_both_themes_have(self):
        roles = set(tokens.themes()["light"])
        for fg, bg, _, where in contrast.pairs():
            self.assertIn(fg, roles, where)
            self.assertIn(bg, roles, where)

    def test_strict_fails_exactly_when_a_pair_is_below_the_bar(self):
        failing = sum(r < need for theme in tokens.themes().values() for _, _, r, need, _ in contrast.rows(theme))
        lines = []
        self.assertEqual(contrast.main([], out=lines.append), 0)
        self.assertEqual(lines[-1], f"{failing} pair(s) below WCAG AA")
        self.assertEqual(contrast.main(["--strict"], out=lambda _: None), 1 if failing else 0)


if __name__ == "__main__":
    unittest.main()
