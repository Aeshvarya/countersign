"""V6: change events on cue. The reset that lets the demo run again, scripts/change_event.py,
and proof on the real /acp path that a change charges nothing."""

import unittest
from decimal import Decimal

from countersign import catalog_gmc
from countersign.merchant import synthetic_catalog

try:
    from tests import test_console as tc
except ImportError:  # run by discover with -s tests
    import test_console as tc

if tc.TestClient is not None:
    from countersign.web import levers

ORIGINAL = {p.sku: p for p in synthetic_catalog()}


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


if __name__ == "__main__":
    unittest.main()
