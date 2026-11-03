"""C10 amended (W1): Airwallex's create_checkout, from the examples in its guide of 2026-11-03."""

import base64
import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from countersign import receipt, tap
from countersign.fx import FakeFx
from countersign.merchant import FakeMerchant
from countersign.terms import money, terms_hash

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from countersign.edge import create_app, hmac_auth
    from countersign.edge.acp import QUOTE_TTL, AcpConfig, to_minor
    from countersign.web.host import Host, house_from_env
except ImportError:  # the server extra is not installed
    TestClient = None

SECRET = b"k" * 32
CHECKOUT = "/acp/create_checkout"
PUBLIC_URL = "https://shop.example"

# The guide's request and response examples (airwallexdev.com/agentic-commerce, "Merchant Create Checkout").
GUIDE_REQUEST = {
    "line_items": [{"id": "li_1", "item_id": "sku_shirt", "requested_quantity": 1}],
    "currency": "USD",
    "customer": {"id": "cus_hkpdxxxxxxxx", "email": "jane@example.com"},
    "shipping_address": {
        "street": "123 Market Street", "city": "San Francisco",
        "state": "CA", "postcode": "94103", "country_code": "US",
    },
}
GUIDE_RESPONSE_KEYS = {"id", "line_items", "fulfillment", "totals", "permalink_url"}
TOTAL_TYPES = {"subtotal", "tax", "fee", "total"}
OPTION_KEYS = {"id", "title", "cost", "carrier", "description", "earliest_delivery_at", "latest_delivery_at"}
METHOD_TYPES = ("SHIPPING", "PICKUP", "DIGITAL", "LOCAL_DELIVERY")


def airwallex_rejects(response):
    """The codes Airwallex fails a checkout with when a response breaks its fulfillment rules (guide, 11-03)."""
    methods = response.get("fulfillment", {}).get("methods", [])
    types = [m["type"] for m in methods]
    cart = {line["id"] for line in response["line_items"]}
    ids = ([m["id"] for m in methods] + [d["id"] for m in methods for d in m.get("destinations", [])]
           + [o["id"] for m in methods for o in m.get("options", [])])
    codes = []
    if any(t not in METHOD_TYPES for t in types):
        codes.append("unsupported_fulfillment_method_type")
    if len(types) != len(set(types)):
        codes.append("multiple_fulfillment_methods_per_type")
    if any(not cart <= set(m["line_item_ids"]) for m in methods):
        codes.append("fulfillment_does_not_cover_cart")
    if len(ids) != len(set(ids)):
        codes.append("duplicate_fulfillment_id")
    return codes


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class NoRateFx:
    """A merchant with no rate for any shopper currency."""

    def lock(self, merchant_currency, shopper_currency, now):
        raise LookupError(f"no rate for {shopper_currency}")


class DownFx:
    """Airwallex not answering the rate lock."""

    def lock(self, merchant_currency, shopper_currency, now):
        raise TimeoutError("no answer")


@unittest.skipIf(TestClient is None, "needs the server extra: pip install -e '.[server]' httpx")
class CreateCheckoutTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.fx = FakeFx("84.10")
        self.build(self.fx)

    def build(self, fx, public_url=PUBLIC_URL):
        self.merchant = FakeMerchant(fx)
        self.app = create_app(self.merchant, tap.StaticDirectory({}), authority="testserver", clock=self.clock,
                              signer=receipt.MerchantSigner(Ed25519PrivateKey.generate(), "m-1"),
                              acp=AcpConfig(secret=SECRET, public_url=public_url))
        self.api = self.app.state.acp
        self.client = TestClient(self.app)
        self.sku, self.other = list(self.merchant.catalog)[:2]

    def post(self, body, *, secret=SECRET, sign=True, raw=None, headers=None):
        raw = json.dumps(body).encode() if raw is None else raw
        headers = {"content-type": "application/json", **(headers or {})}
        if sign:
            headers.update(hmac_auth.sign(raw, secret))
        return self.client.post(CHECKOUT, content=raw, headers=headers)

    def checkout(self, *lines, currency="INR", **extra):
        body = {"line_items": [{"id": f"li_{i + 1}", "item_id": sku, "requested_quantity": qty}
                               for i, (sku, qty) in enumerate(lines)], "currency": currency, **extra}
        r = self.post(body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def guide_request(self, sku=None):
        body = copy.deepcopy(GUIDE_REQUEST)
        if sku is not None:
            body["line_items"][0]["item_id"] = sku
        return body

    def totals(self, response):
        return {t["type"]: t["amount"] for t in response["totals"]}

    def price(self, cart, currency):
        """What the cart costs in minor units of `currency` at the fake rate, rounded once on the total."""
        merchant_total = sum(self.merchant.catalog[sku].price * qty for sku, qty in cart.items())
        rate = Decimal(1) if currency == self.merchant.currency else self.fx.rate
        return to_minor(money(merchant_total * rate, currency), currency)

    # -- authentication: Airwallex's signature, x-timestamp + raw body

    def test_unsigned_is_refused(self):
        r = self.post(self.guide_request(self.sku), sign=False)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.api.quotes, {})

    def test_another_secret_is_refused(self):
        self.assertEqual(self.post(self.guide_request(self.sku), secret=b"x" * 32).status_code, 401)

    def test_the_signature_covers_the_raw_body(self):
        signed = json.dumps(self.guide_request(self.sku)).encode()
        sent = json.dumps({**self.guide_request(self.sku), "currency": "INR"}).encode()
        headers = {"content-type": "application/json", **hmac_auth.sign(signed, SECRET)}
        self.assertEqual(self.client.post(CHECKOUT, content=sent, headers=headers).status_code, 401)

    def test_no_idempotency_key_and_no_tap_signature_needed(self):
        self.assertEqual(self.post(self.guide_request(self.sku)).status_code, 200)

    def test_an_api_version_header_is_not_ours_to_check_here(self):
        r = self.post(self.guide_request(self.sku), headers={"API-Version": "2099-01-01"})
        self.assertEqual(r.status_code, 200, r.text)

    # -- the guide's examples

    def test_the_guide_request_for_an_item_we_do_not_sell(self):
        out = self.post(GUIDE_REQUEST).json()
        self.assertTrue(out["id"].startswith("chk_"))
        self.assertEqual(out["line_items"], [{"id": "li_1", "item_id": "sku_shirt", "requested_quantity": 1,
                                              "available_quantity": 0}])
        self.assertEqual(self.totals(out), {"subtotal": 0, "total": 0})
        self.assertNotIn("permalink_url", out)  # no product page to link to

    def test_the_guide_request_answers_in_the_guide_response_shape(self):
        r = self.post(self.guide_request(self.sku))
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertLessEqual(set(out), GUIDE_RESPONSE_KEYS)
        self.assertIsInstance(out["id"], str)
        self.assertEqual(out["line_items"], [{"id": "li_1", "item_id": self.sku, "requested_quantity": 1,
                                              "available_quantity": 1}])
        self.assertLessEqual({t["type"] for t in out["totals"]}, TOTAL_TYPES)
        self.assertLessEqual({"subtotal", "total"}, {t["type"] for t in out["totals"]})
        for row in out["totals"]:
            self.assertEqual(set(row), {"type", "amount", "display_text"})
            self.assertIs(type(row["amount"]), int)
        self.assertEqual(self.totals(out), {"subtotal": self.price({self.sku: 1}, "USD"),
                                            "total": self.price({self.sku: 1}, "USD")})
        self.assertEqual(out["permalink_url"], f"{PUBLIC_URL}/p/{self.sku}")

    # -- pricing: the live catalogue, a fresh rate lock per call

    def test_every_call_is_a_fresh_quote_with_its_own_rate_lock(self):
        first = self.checkout((self.sku, 2))
        self.fx.rate = Decimal("85.00")
        second = self.checkout((self.sku, 2))
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.totals(first)["total"], to_minor(money(
            self.merchant.catalog[self.sku].price * 2 * Decimal("84.10"), "INR"), "INR"))
        self.assertEqual(self.totals(second)["total"], self.price({self.sku: 2}, "INR"))
        locks = {self.api.quote_for(c["id"]).terms.fx.quote_id for c in (first, second)}
        self.assertEqual(len(locks), 2)

    def test_the_live_price_is_what_is_quoted(self):
        self.merchant.set_price(self.sku, self.merchant.catalog[self.sku].price * Decimal("1.12"))
        out = self.checkout((self.sku, 1))
        self.assertEqual(self.totals(out)["total"], self.price({self.sku: 1}, "INR"))

    def test_the_merchant_currency_needs_no_rate_lock(self):
        issued = self.fx.issued
        out = self.checkout((self.sku, 1), currency="USD")
        self.assertEqual(self.fx.issued, issued)
        self.assertIsNone(self.api.quote_for(out["id"]).terms.fx)

    # -- availability: answered, never an error

    def test_sold_out_is_available_zero(self):
        self.merchant.set_stock(self.sku, 0)
        out = self.checkout((self.sku, 1))
        self.assertEqual(out["line_items"][0]["available_quantity"], 0)
        self.assertEqual(self.totals(out), {"subtotal": 0, "total": 0})
        self.assertIsNone(self.api.quote_for(out["id"]).terms)

    def test_short_stock_is_adjusted_and_priced_as_adjusted(self):
        self.merchant.set_stock(self.sku, 2)
        out = self.checkout((self.sku, 5))
        self.assertEqual(out["line_items"][0]["available_quantity"], 2)
        self.assertEqual(self.totals(out)["total"], self.price({self.sku: 2}, "INR"))

    def test_two_lines_for_one_sku_share_its_stock(self):
        self.merchant.set_stock(self.sku, 3)
        out = self.checkout((self.sku, 2), (self.sku, 2))
        self.assertEqual([line["available_quantity"] for line in out["line_items"]], [2, 1])
        self.assertEqual(self.totals(out)["total"], self.price({self.sku: 3}, "INR"))

    def test_an_unknown_item_does_not_stop_the_rest(self):
        out = self.checkout(("sku_shirt", 1), (self.sku, 1))
        self.assertEqual([line["available_quantity"] for line in out["line_items"]], [0, 1])
        self.assertEqual(self.totals(out)["total"], self.price({self.sku: 1}, "INR"))
        self.assertEqual(out["permalink_url"], f"{PUBLIC_URL}/p/{self.sku}")  # the first product we sell

    # -- the quote record the payment webhook reads (W3)

    def test_the_quote_record(self):
        out = self.checkout((self.sku, 2), (self.other, 1), customer=GUIDE_REQUEST["customer"],
                            shipping_address=GUIDE_REQUEST["shipping_address"])
        q = self.api.quote_for(out["id"])
        self.assertEqual(self.api.quotes[out["id"]], [q])
        self.assertEqual((q.checkout_id, q.created, q.currency), (out["id"], self.clock.now, "INR"))
        self.assertEqual(list(q.line_items), out["line_items"])
        self.assertEqual(q.total_minor, self.totals(out)["total"])
        self.assertEqual(q.terms.fx.quote_id, f"cvq_fake_{self.fx.issued}")
        self.assertEqual({i.sku: i.quantity for i in q.terms.items}, {self.sku: 2, self.other: 1})
        self.assertEqual(q.terms_hash, terms_hash(q.terms, q.terms.shopper_total))
        self.assertEqual(q.approval.hash, q.terms_hash)
        self.assertEqual(q.approval.approver, "acp:jane@example.com")
        self.assertEqual(q.customer, GUIDE_REQUEST["customer"])
        self.assertEqual(q.shipping_address, GUIDE_REQUEST["shipping_address"])
        self.assertEqual(q.expires_at, self.clock.now + QUOTE_TTL)
        self.assertIsNone(self.api.quote_for("chk_unknown"))

    def test_a_quote_expires_with_its_rate_lock(self):
        self.fx.validity = timedelta(minutes=10)
        q = self.api.quote_for(self.checkout((self.sku, 1))["id"])
        self.assertEqual(q.expires_at, self.clock.now + timedelta(minutes=10))
        self.assertEqual(q.approval.expires_at, q.expires_at)

    def test_unknown_customer_and_address_fields_are_not_kept(self):
        out = self.checkout((self.sku, 1), customer={"email": "a@example.com", "password": "x"},
                            shipping_address={"city": "Pune", "gate_code": "1234"})
        q = self.api.quote_for(out["id"])
        self.assertEqual((q.customer, q.shipping_address), ({"email": "a@example.com"}, {"city": "Pune"}))

    def test_the_shoppers_details_are_never_logged(self):
        with self.assertLogs("countersign.acp", level="WARNING") as logs:
            self.checkout((self.sku, 1), customer=GUIDE_REQUEST["customer"],
                          shipping_address=GUIDE_REQUEST["shipping_address"])
        text = "\n".join(logs.output)
        self.assertIn("body keys", text)
        for value in ("jane@example.com", "123 Market Street", "94103", "cus_hkpdxxxxxxxx"):
            self.assertNotIn(value, text)

    # -- fulfillment: one SHIPPING method, standard and express, never in the total

    def express(self, currency):
        rate = Decimal(1) if currency == self.merchant.currency else self.fx.rate
        return to_minor(money(Decimal("9.99") * rate, currency), currency)

    def test_one_shipping_method_with_standard_and_express(self):
        out = self.checkout((self.sku, 1), (self.other, 2), shipping_address=GUIDE_REQUEST["shipping_address"])
        [method] = out["fulfillment"]["methods"]
        self.assertEqual((method["id"], method["type"]), ("shipping", "SHIPPING"))
        self.assertEqual(method["line_item_ids"], ["li_1", "li_2"])
        self.assertEqual(method["destinations"], [{"id": "shipping_address",
                                                   "address": GUIDE_REQUEST["shipping_address"]}])
        self.assertEqual([(o["id"], o["cost"]) for o in method["options"]],
                         [("standard", 0), ("express", self.express("INR"))])
        self.assertEqual(self.express("INR"), 84016)  # 9.99 USD at 84.10, rounded once
        for option in method["options"]:
            self.assertLessEqual(set(option), OPTION_KEYS)
            self.assertIs(type(option["cost"]), int)
            self.assertIn("business days", option["title"])

    def test_without_an_address_there_is_no_destination(self):
        [method] = self.checkout((self.sku, 1))["fulfillment"]["methods"]
        self.assertEqual(method["destinations"], [])

    def test_the_total_never_includes_fulfillment(self):
        out = self.checkout((self.sku, 1))
        self.assertNotIn("fulfillment", {t["type"] for t in out["totals"]})
        self.assertEqual(self.totals(out)["total"], self.price({self.sku: 1}, "INR"))

    def test_the_quote_keeps_what_each_option_costs(self):
        for currency in ("INR", "USD"):
            with self.subTest(currency=currency):
                q = self.api.quote_for(self.checkout((self.sku, 1), currency=currency)["id"])
                self.assertEqual(q.options, {"standard": 0, "express": self.express(currency)})
        self.assertEqual(self.express("USD"), 999)

    def test_nothing_available_means_nothing_to_ship(self):
        self.merchant.set_stock(self.sku, 0)
        out = self.checkout((self.sku, 1))
        self.assertNotIn("fulfillment", out)
        self.assertEqual(self.api.quote_for(out["id"]).options, {})

    def test_no_answer_breaks_airwallex_fulfillment_rules(self):
        self.merchant.set_stock(self.other, 1)
        scenarios = {
            "one line": [(self.sku, 1)],
            "two lines": [(self.sku, 1), (self.other, 1)],
            "one sku twice": [(self.sku, 1), (self.sku, 2)],
            "short stock": [(self.other, 3), (self.sku, 1)],
            "an unknown item": [("sku_shirt", 1), (self.sku, 1)],
        }
        for name, lines in scenarios.items():
            for extra in ({}, {"shipping_address": GUIDE_REQUEST["shipping_address"]}):
                for currency in ("INR", "USD"):
                    with self.subTest(name, address=bool(extra), currency=currency):
                        out = self.checkout(*lines, currency=currency, **extra)
                        self.assertIn("fulfillment", out)
                        self.assertEqual(airwallex_rejects(out), [])

    def test_the_rule_check_catches_each_failure(self):
        good = self.checkout((self.sku, 1), (self.other, 1), shipping_address=GUIDE_REQUEST["shipping_address"])
        self.assertEqual(airwallex_rejects(good), [])

        def broken(change):
            out = copy.deepcopy(good)
            change(out["fulfillment"]["methods"])
            return airwallex_rejects(out)

        def second_shipping(methods):
            methods.append({**copy.deepcopy(methods[0]), "id": "ship2", "destinations": [],
                            "options": [{"id": "slow", "title": "Slow", "cost": 0}]})

        def unknown_type(methods):
            methods[0]["type"] = "COURIER"

        def misses_a_line(methods):
            methods[0]["line_item_ids"].pop()

        def option_reuses_an_id(methods):
            methods[0]["options"][1]["id"] = methods[0]["destinations"][0]["id"]

        self.assertEqual(broken(second_shipping), ["multiple_fulfillment_methods_per_type"])
        self.assertEqual(broken(unknown_type), ["unsupported_fulfillment_method_type"])
        self.assertEqual(broken(misses_a_line), ["fulfillment_does_not_cover_cart"])
        self.assertEqual(broken(option_reuses_an_id), ["duplicate_fulfillment_id"])

    # -- refusals

    def test_malformed_bodies_name_the_field(self):
        line = {"id": "li_1", "item_id": "x", "requested_quantity": 1}
        cases = {
            "$.line_items": [{"currency": "USD"}, {"line_items": [], "currency": "USD"},
                             {"line_items": {"id": "li_1"}, "currency": "USD"}],
            "$.line_items[0]": [{"line_items": ["li_1"], "currency": "USD"}],
            "$.line_items[0].id": [{"line_items": [{**line, "id": 7}], "currency": "USD"}],
            "$.line_items[0].item_id": [{"line_items": [{**line, "item_id": ""}], "currency": "USD"}],
            "$.line_items[0].requested_quantity": [
                {"line_items": [{**line, "requested_quantity": q}], "currency": "USD"} for q in (0, -1, "1", True, 1.5)],
            "$.line_items[1].id": [{"line_items": [line, {**line, "item_id": "y"}], "currency": "USD"}],
            "$.currency": [{"line_items": [line]}, {"line_items": [line], "currency": "US"}],
            "$.customer": [{"line_items": [line], "currency": "USD", "customer": "jane"}],
            "$.shipping_address.city": [{"line_items": [line], "currency": "USD", "shipping_address": {"city": 7}}],
        }
        for param, bodies in cases.items():
            for body in bodies:
                with self.subTest(param=param, body=body):
                    r = self.post(body)
                    self.assertEqual(r.status_code, 400, r.text)
                    self.assertEqual(r.json()["param"], param)
        self.assertEqual(self.api.quotes, {})

    def test_a_body_that_is_not_an_object(self):
        self.assertEqual(self.post(None, raw=b"[1]").status_code, 400)
        self.assertEqual(self.post(None, raw=b"{").status_code, 400)

    def test_a_currency_we_cannot_price(self):
        self.build(NoRateFx())
        r = self.post({"line_items": [{"id": "li_1", "item_id": self.sku, "requested_quantity": 1}],
                       "currency": "INR"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()["param"], "$.currency")

    def test_no_rate_lock_is_a_503_to_retry(self):
        self.build(DownFx())
        r = self.post({"line_items": [{"id": "li_1", "item_id": self.sku, "requested_quantity": 1}],
                       "currency": "INR"})
        self.assertEqual(r.status_code, 503, r.text)
        self.assertEqual(r.json()["code"], "rate_lock_unavailable")
        self.assertEqual(r.headers["retry-after"], "5")
        self.assertEqual(self.api.quotes, {})

    def test_no_permalink_without_a_public_url(self):
        self.build(self.fx, public_url=None)
        self.assertNotIn("permalink_url", self.checkout((self.sku, 1)))


@unittest.skipIf(TestClient is None, "needs the server extra: pip install -e '.[server]' httpx")
class HostedTest(unittest.TestCase):
    def test_the_public_host_serves_create_checkout_outside_tap(self):
        house = house_from_env({"COUNTERSIGN_PUBLIC_URL": "https://countersign-demo.onrender.com",
                                "COUNTERSIGN_MERCHANT_SIGNING_KEY": base64.b64encode(bytes(range(32))).decode(),
                                "COUNTERSIGN_ACP_HMAC_SECRET": "s" * 32})
        client = TestClient(Host(FastAPI(), house), base_url="https://countersign-demo.onrender.com")
        sku = next(iter(house.merchant.catalog))
        raw = json.dumps({"line_items": [{"id": "li_1", "item_id": sku, "requested_quantity": 1}],
                          "currency": "USD"}).encode()
        unsigned = client.post(CHECKOUT, content=raw, headers={"content-type": "application/json"})
        self.assertEqual(unsigned.status_code, 401)
        r = client.post(CHECKOUT, content=raw,
                        headers={"content-type": "application/json", **hmac_auth.sign(raw, b"s" * 32)})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["permalink_url"], f"https://countersign-demo.onrender.com/p/{sku}")


if __name__ == "__main__":
    unittest.main()
