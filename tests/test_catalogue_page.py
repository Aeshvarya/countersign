"""V8: the catalogue page reads well on any screen and takes its type and spacing from the token file too.

Checked on the rendered page, not on a picture of it: the rules a page can break
without anyone noticing until a phone, a keyboard or a long product name finds it.
"""

import re
import unittest
from dataclasses import replace

from countersign.catalog_gmc import MERCHANT_NAME, description, gmc_price
from countersign.edge import pages, tokens
from countersign.merchant import synthetic_catalog


def _own_css(html: str) -> str:
    """The page's own rules: its <style> without the inlined token blocks."""
    style = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
    return style.replace(tokens.css(), "")


def _visible_text(html: str) -> str:
    body = re.search(r"<body>(.*)</body>", html, re.S).group(1)
    return re.sub(r"<[^>]+>", " ", body)


class CataloguePageCraftTest(unittest.TestCase):
    def setUp(self):
        self.product = synthetic_catalog()[0]
        self.html = pages.product_page(self.product)
        self.css = _own_css(self.html)

    def test_every_font_and_size_comes_from_a_token(self):
        for family in re.findall(r"font-family:\s*([^;]+);", self.css):
            self.assertTrue(family.strip().startswith("var(--font-"), family)
        for shorthand in re.findall(r"\bfont:\s*([^;]+);", self.css):
            self.assertIn("var(--font-", shorthand)
        for size in re.findall(r"font-size:\s*([^;]+);", self.css):
            self.assertTrue(size.strip().startswith("var(--step"), size)

    def test_nothing_scrolls_sideways_or_animates_layout(self):
        self.assertRegex(self.css, r"html,\s*body\s*\{[^}]*overflow-x:\s*clip")
        self.assertNotIn("overflow-x: hidden", self.css)
        self.assertNotIn("100vw", self.css)
        self.assertNotRegex(self.css, r"transition:\s*all")
        self.assertIn("prefers-reduced-motion: reduce", self.css)

    def test_the_layout_holds_a_long_name_and_a_wide_screen(self):
        self.assertIn("minmax(0, 1fr)", self.css)
        self.assertRegex(self.css, r"h1\s*\{[^}]*overflow-wrap:\s*break-word[^}]*min-width:\s*0")
        self.assertIn("max-width: var(--container)", self.css)

    def test_a_keyboard_sees_where_it_is(self):
        self.assertRegex(self.css, r":focus-visible\s*\{[^}]*outline:")

    def test_the_shop_window_shows_every_field_the_catalogue_row_links_to(self):
        text = _visible_text(self.html)
        self.assertIn(self.product.title, text)
        self.assertIn(gmc_price(self.product.price, "USD"), text)
        self.assertIn("In stock", text)
        self.assertIn(description(self.product), text)
        self.assertIn(self.product.sku, text)
        self.assertIn(MERCHANT_NAME, text)
        self.assertIn(f'src="/img/{self.product.sku}.svg"', self.html)
        self.assertIn(f'alt="{self.product.title}"', self.html)

    def test_facts_are_a_list_not_a_dotted_string(self):
        text = _visible_text(self.html)
        self.assertNotIn(" \u00b7 ", text)
        self.assertIn("<dl", self.html)

    def test_availability_is_said_in_words(self):
        sold_out = pages.product_page(replace(self.product, stock=0))
        self.assertIn("Out of stock", _visible_text(sold_out))
        self.assertNotIn("In stock", _visible_text(sold_out))

    def test_the_price_keeps_its_width_when_digits_change(self):
        self.assertRegex(self.css, r"\.price\s*\{[^}]*font-variant-numeric:\s*tabular-nums")

    def test_one_edge_no_glow(self):
        self.assertNotIn("box-shadow", self.css)
        self.assertNotIn("gradient", self.css)
        for radius in re.findall(r"border-radius:\s*([^;]+);", self.css):
            self.assertTrue(radius.strip().startswith("var(--radius-"), radius)

    def test_no_label_is_set_in_tracked_capitals(self):
        self.assertNotIn("text-transform: uppercase", self.css)
        self.assertNotIn("letter-spacing", self.css)
        self.assertNotIn("small-caps", self.css)


class ProductImageTypeTest(unittest.TestCase):
    def test_the_image_draws_its_words_in_the_page_faces(self):
        svg = pages.product_svg(synthetic_catalog()[0])
        families = re.findall(r'font-family="([^"]+)"', svg)
        self.assertEqual(families[0], tokens.font("display"))  # the initials
        self.assertEqual(set(families[1:]), {tokens.font("sans")})  # the name and the variant


if __name__ == "__main__":
    unittest.main()
