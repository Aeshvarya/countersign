"""V4′: the product pages and images the catalogue rows link to are public; nothing else is."""

import unittest
from dataclasses import replace
from datetime import datetime, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from countersign import receipt, tap
from countersign.fx import FakeFx
from countersign.merchant import FakeMerchant, synthetic_catalog

try:
    from fastapi.testclient import TestClient

    from countersign.edge import create_app
    from countersign.edge import pages
except ImportError:  # the server extra is not installed
    TestClient = None

AGENT = "https://agents.example"


@unittest.skipIf(TestClient is None, "needs the server extra: pip install -e '.[server]' httpx")
class PublicPagesTest(unittest.TestCase):
    def setUp(self):
        self.merchant = FakeMerchant(FakeFx("84.10"))
        key = Ed25519PrivateKey.generate()
        directory = tap.StaticDirectory({(AGENT, "k1"): key.public_key()})
        self.app = create_app(self.merchant, directory, authority="testserver",
                              clock=lambda: datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc),
                              signer=receipt.MerchantSigner(Ed25519PrivateKey.generate(), "m-1"))
        self.client = TestClient(self.app)
        self.sku = next(iter(self.merchant.catalog))
        self.product = self.merchant.catalog[self.sku]

    def test_a_product_page_answers_without_a_signature(self):
        r = self.client.get(f"/p/{self.sku}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers["content-type"])
        self.assertIn(self.product.title, r.text)
        self.assertIn(f"{self.product.price:.2f} USD", r.text)
        self.assertIn("In stock", r.text)

    def test_a_product_image_answers_without_a_signature(self):
        r = self.client.get(f"/img/{self.sku}.svg")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "image/svg+xml")
        self.assertTrue(r.text.startswith("<svg"))
        self.assertEqual(r.text, self.client.get(f"/img/{self.sku}.svg").text)  # deterministic

    def test_pages_show_the_live_catalogue(self):
        self.merchant.set_price(self.sku, "9.99")
        self.merchant.set_stock(self.sku, 0)
        r = self.client.get(f"/p/{self.sku}")
        self.assertIn("9.99 USD", r.text)
        self.assertIn("Out of stock", r.text)

    def test_an_unknown_product_is_a_404_not_a_refusal(self):
        self.assertEqual(self.client.get("/p/NOP-000-0").status_code, 404)
        self.assertEqual(self.client.get("/img/NOP-000-0.svg").status_code, 404)

    def test_everything_else_still_needs_a_signature(self):
        self.assertEqual(self.client.get("/products").status_code, 401)
        self.assertEqual(self.client.post("/offers", json={"cart": {self.sku: 1}, "shopper_currency": "INR"}).status_code, 401)
        self.assertEqual(self.client.post("/checkout", json={"approval": {}, "idempotency_key": "k"}).status_code, 401)
        self.assertEqual(self.client.get("/orders/k").status_code, 401)

    def test_the_exemption_is_exact(self):
        # Only GET/HEAD, only the two path shapes: no prefix tricks, no other verbs.
        self.assertEqual(self.client.post(f"/p/{self.sku}", json={}).status_code, 401)
        for path in (f"/p/{self.sku}/x", f"/img/{self.sku}.svg.png", "/p/", "/img/.svg", f"/p/{self.sku}%2F..%2Fcheckout"):
            self.assertEqual(self.client.get(path).status_code, 401, path)

    def test_page_text_is_escaped(self):
        hostile = replace(synthetic_catalog()[0], title="<script>alert(1)</script> Pro")
        app = create_app(self.merchant, tap.StaticDirectory({}), authority="testserver",
                         products=lambda sku: hostile if sku == hostile.sku else None)
        client = TestClient(app)
        for path in (f"/p/{hostile.sku}", f"/img/{hostile.sku}.svg"):
            body = client.get(path).text
            self.assertNotIn("<script>", body, path)
            self.assertIn("&lt;script&gt;", body, path)


class IsPublicTest(unittest.TestCase):
    @unittest.skipIf(TestClient is None, "needs the server extra")
    def test_shapes(self):
        self.assertTrue(pages.is_public("GET", "/p/AUD-123-0"))
        self.assertTrue(pages.is_public("HEAD", "/img/AUD-123-0.svg"))
        self.assertFalse(pages.is_public("POST", "/p/AUD-123-0"))
        self.assertFalse(pages.is_public("GET", "/checkout"))
        self.assertFalse(pages.is_public("GET", "/p/AUD-123-0/../../checkout"))
        self.assertFalse(pages.is_public("GET", "/img/AUD-123-0.png"))


if __name__ == "__main__":
    unittest.main()
