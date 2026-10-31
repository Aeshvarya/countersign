"""V6: change events on cue. The reset that lets the demo run again, scripts/change_event.py,
and proof on the real /acp path that a change charges nothing."""

import importlib.util
import pathlib
import sys
import unittest
import unittest.mock
from decimal import Decimal

from countersign import catalog_gmc
from countersign.airwallex_merchant import AirwallexMerchant
from countersign.fx import FakeFx
from countersign.merchant import synthetic_catalog

try:
    from tests import test_console as tc
except ImportError:  # run by discover with -s tests
    import test_console as tc

if tc.TestClient is not None:
    from countersign.web import levers

_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "change_event.py"
_spec = importlib.util.spec_from_file_location("change_event", _PATH)
change_event = importlib.util.module_from_spec(_spec)
sys.modules["change_event"] = change_event
_spec.loader.exec_module(change_event)

ORIGINAL = {p.sku: p for p in synthetic_catalog()}
TOKEN = {"COUNTERSIGN_LEVER_TOKEN": tc.LEVER}


def group_of(sku):
    return sorted(s for s in ORIGINAL if catalog_gmc.group_id(s) == catalog_gmc.group_id(sku))


@unittest.skipIf(tc.TestClient is None, "needs the server extra")
class ResetTest(tc.HouseCase):
    def setUp(self):
        super().setUp()
        self.a, self.b = group_of(self.house.merchant.search("kettle")[0].sku)[:2]  # two variants, one product
        self.other = next(s for s in ORIGINAL if catalog_gmc.group_id(s) != catalog_gmc.group_id(self.a))

    def test_every_moved_product_goes_back_as_uploaded(self):
        levers.set_price(self.house, self.a, "99.99")
        levers.set_stock(self.house, self.a, 0)
        levers.set_stock(self.house, self.b, 1)
        levers.set_delivery(self.house, self.other, "slow boat")
        changes = levers.reset(self.house)
        self.assertEqual(sorted((c.sku, c.field) for c in changes),
                         sorted([(self.a, "price"), (self.a, "stock"), (self.b, "stock"), (self.other, "delivery")]))
        for sku in (self.a, self.b, self.other):
            now, was = self.house.merchant.catalog[sku], ORIGINAL[sku]
            self.assertEqual((now.price, now.stock, now.delivery), (was.price, was.stock, was.delivery))
        self.assertTrue(any(t.startswith("Merchant reset") for t in self.titles()))

    def test_one_product_only(self):
        levers.set_stock(self.house, self.a, 0)
        levers.set_stock(self.house, self.other, 0)
        self.assertEqual([(c.sku, c.after) for c in levers.reset(self.house, self.a)],
                         [(self.a, str(ORIGINAL[self.a].stock))])
        self.assertEqual(self.house.merchant.catalog[self.other].stock, 0)

    def test_nothing_moved_means_no_change_and_no_upsert(self):
        self.house.airwallex, self.house.sync_catalog = tc.FakeCatalogClient(), True
        self.assertEqual(levers.reset(self.house), [])
        self.assertEqual(self.house.airwallex.calls, [])

    def test_each_product_is_upserted_once_after_all_its_fields_are_back(self):
        levers.set_price(self.house, self.a, "99.99")
        levers.set_stock(self.house, self.b, 0)
        levers.set_delivery(self.house, self.other, "slow boat")
        self.house.airwallex, self.house.sync_catalog = tc.FakeCatalogClient(), True
        levers.reset(self.house)
        calls = self.house.airwallex.calls
        self.assertEqual(len(calls), 2)  # one per product group, not one per field or variant
        rows = {r["id"]: r for _, body in calls for r in body["gmc_products"]}
        self.assertEqual(sorted(rows), sorted(group_of(self.a) + group_of(self.other)))
        self.assertEqual(rows[self.a]["price"], catalog_gmc.gmc_price(ORIGINAL[self.a].price, "USD"))
        self.assertEqual(rows[self.b]["availability"], "in_stock")

    def test_an_unknown_product_is_refused(self):
        with self.assertRaises(levers.LeverError):
            levers.reset(self.house, "NOPE-000-0")

    def test_the_route(self):
        levers.set_stock(self.house, self.a, 0)
        self.assertEqual(self.client.post("/api/levers/reset", json={}).status_code, 401)
        r = self.client.post("/api/levers/reset", json={"sku": self.a}, headers=self.auth())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([(c["sku"], c["field"]) for c in r.json()["changes"]], [(self.a, "stock")])
        r = self.client.post("/api/levers/reset", json={}, headers=self.auth())
        self.assertEqual(r.json(), {"changes": []})
        self.assertEqual(self.client.post("/api/levers/reset", json={"sku": "x"}, headers=self.auth()).status_code, 400)


class RowsFailClient(tc.FakeCatalogClient):
    """Airwallex answers, but refuses some rows: records_failed > 0."""

    def post(self, path, body):
        self.calls.append((path, body))
        return {"records_updated": len(body["gmc_products"]) - 1, "records_failed": 1}


class Unreachable:
    def post(self, *args, **kwargs):
        raise ConnectionError("no route to host")


@unittest.skipIf(tc.TestClient is None, "needs the server extra")
class ScriptTest(tc.HouseCase):
    def setUp(self):
        super().setUp()
        self.sku = self.house.merchant.search("kettle")[0].sku
        self.lines = []

    def run_script(self, *argv, environ=TOKEN, client=None):
        return change_event.main(list(argv), environ=environ, client=client or self.client, out=self.lines.append)

    def test_a_price_change_lands_on_the_host_and_in_airwallex(self):
        self.house.airwallex, self.house.sync_catalog = tc.FakeCatalogClient(), True
        self.assertEqual(self.run_script("--sku", self.sku, "--price", "54.99"), 0, self.lines)
        self.assertEqual(self.house.merchant.catalog[self.sku].price, Decimal("54.99"))
        (_, body), = self.house.airwallex.calls
        row = next(r for r in body["gmc_products"] if r["id"] == self.sku)
        self.assertEqual(row["price"], "54.99 USD")
        self.assertTrue(self.lines[0].startswith(f"CHANGED  {self.sku} price"))
        self.assertIn("Airwallex catalogue updated", self.lines[-1])

    def test_percent_stock_and_delivery(self):
        was = self.house.merchant.catalog[self.sku].price
        self.assertEqual(self.run_script("--sku", self.sku, "--percent", "10"), 0)
        self.assertGreater(self.house.merchant.catalog[self.sku].price, was)
        self.assertEqual(self.run_script("--sku", self.sku, "--stock", "0"), 0)
        self.assertEqual(self.house.merchant.catalog[self.sku].stock, 0)
        self.assertEqual(self.run_script("--sku", self.sku, "--delivery", "express, 2 business days"), 0)
        self.assertEqual(self.house.merchant.catalog[self.sku].delivery, "express, 2 business days")

    def test_without_sync_it_says_airwallex_was_not_asked(self):
        self.assertEqual(self.run_script("--sku", self.sku, "--stock", "3"), 0)
        self.assertIn("COUNTERSIGN_SYNC_CATALOG", self.lines[-1])

    def test_a_failed_upsert_exits_1_and_says_which_side_changed(self):
        self.house.airwallex, self.house.sync_catalog = tc.FakeCatalogClient(fail=True), True
        self.assertEqual(self.run_script("--sku", self.sku, "--stock", "0"), 1)
        self.assertEqual(self.house.merchant.catalog[self.sku].stock, 0)
        self.assertIn("NOT updated", self.lines[-1])
        self.assertIn("the host has the change, Airwallex does not", self.lines[-1])

    def test_failed_rows_count_as_a_failed_upsert(self):
        self.house.airwallex, self.house.sync_catalog = RowsFailClient(), True
        self.assertEqual(self.run_script("--sku", self.sku, "--stock", "0"), 1)
        self.assertIn("1 rows failed", self.lines[-1])

    def test_reset_all_and_one(self):
        self.house.airwallex, self.house.sync_catalog = tc.FakeCatalogClient(), True
        self.run_script("--sku", self.sku, "--stock", "0")
        self.run_script("--sku", self.sku, "--price", "1.00")
        self.lines.clear()
        self.assertEqual(self.run_script("--reset"), 0, self.lines)
        now, was = self.house.merchant.catalog[self.sku], ORIGINAL[self.sku]
        self.assertEqual((now.price, now.stock), (was.price, was.stock))
        self.assertEqual(sum(line.startswith("CHANGED") for line in self.lines), 2)
        self.assertEqual(sum("Airwallex catalogue updated" in line for line in self.lines), 1)  # one product, one upsert
        self.lines.clear()
        self.assertEqual(self.run_script("--sku", self.sku, "--reset"), 0)
        self.assertEqual(self.lines, ["Nothing to reset: every product is as uploaded."])

    def test_nothing_changes_on_a_refusal(self):
        before = self.house.merchant.catalog[self.sku]
        self.assertEqual(self.run_script("--sku", self.sku, "--stock", "-1"), 2)
        self.assertIn("NOT CHANGED: 400", self.lines[-1])
        self.assertEqual(self.run_script("--sku", self.sku, "--stock", "0", environ={"COUNTERSIGN_LEVER_TOKEN": "x" * 40}), 2)
        self.assertIn("NOT CHANGED: 401", self.lines[-1])
        self.assertEqual(self.run_script("--sku", self.sku, "--stock", "0", client=Unreachable()), 2)
        self.assertIn("the host did not answer (ConnectionError)", self.lines[-1])
        self.assertEqual(self.house.merchant.catalog[self.sku], before)

    def test_the_command_line_is_checked_before_anything_is_sent(self):
        for argv, environ in ((["--stock", "0"], TOKEN),                   # no product
                              (["--sku", self.sku, "--stock", "0"], {}),     # no token
                              (["--sku", self.sku, "--stock", "0", "--price", "2"], TOKEN)):  # two changes at once
            with self.assertRaises(SystemExit) as raised, unittest.mock.patch("sys.stderr"):
                change_event.main(argv, environ=environ, client=Unreachable(), out=self.lines.append)
            self.assertEqual(raised.exception.code, 2)
        self.assertEqual(self.lines, [])


@unittest.skipIf(tc.TestClient is None, "needs the server extra")
class AcpPathTest(tc.HouseCase):
    """R7 on the path Airwallex calls: the shopper approved, then the merchant pulled a lever."""

    def merchant(self):
        self.api = tc.FakeAirwallex()
        return AirwallexMerchant(self.api, FakeFx("84.10"))

    def lever(self, *argv):
        self.assertEqual(change_event.main(list(argv), environ=TOKEN, client=self.client, out=lambda _: None), 0)

    def complete(self, sid):
        return self.acp(f"{tc.SESSIONS}/{sid}/complete", {}).json()

    def codes(self, view):
        return [m["code"] for m in view["messages"]]

    def test_a_price_change_after_approval_asks_again_and_pays_once(self):
        s = self.session()
        self.sign(s["id"])
        self.lever("--sku", self.sku, "--percent", "12")
        view = self.complete(s["id"])
        self.assertIn("price_change", self.codes(view))
        self.assertNotEqual(view["status"], "completed")
        self.assertEqual((self.api.intents, self.house.merchant.orders), ({}, {}))  # nothing charged
        self.sign(s["id"])  # the shopper approves the new total
        self.assertEqual(self.complete(s["id"])["status"], "completed")
        self.complete(s["id"])  # Airwallex retries the call
        self.assertEqual(len(self.api.intents), 1)  # one payment, never two

    def test_an_emptied_stock_answers_out_of_stock_and_charges_nothing(self):
        s = self.session()
        self.sign(s["id"])
        self.lever("--sku", self.sku, "--stock", "0")
        view = self.complete(s["id"])
        self.assertIn("out_of_stock", self.codes(view))
        self.assertNotEqual(view["status"], "completed")
        self.assertEqual((self.api.intents, self.house.merchant.orders), ({}, {}))

    def test_after_a_reset_the_same_purchase_goes_through(self):
        self.lever("--sku", self.house.merchant.search("kettle")[0].sku, "--stock", "0")
        self.lever("--reset")
        s = self.session()
        self.sign(s["id"])
        self.assertEqual(self.complete(s["id"])["status"], "completed")
        self.assertEqual(len(self.api.intents), 1)


if __name__ == "__main__":
    unittest.main()
