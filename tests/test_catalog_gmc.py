"""V4′ / C11: the catalogue as GMC rows, and the upload that never splits a product."""

import unittest
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from countersign import catalog_gmc
from countersign.fx import FakeFx
from countersign.merchant import FakeMerchant, synthetic_catalog

BASE = "https://shop.example"
NOW = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)


class RowsTest(unittest.TestCase):
    def setUp(self):
        self.rows = catalog_gmc.catalog(BASE)

    def test_every_row_carries_every_documented_field(self):
        for row in self.rows:
            self.assertEqual(set(row), set(catalog_gmc.DOCUMENTED_FIELDS))
            for name, value in row.items():
                self.assertIsInstance(value, str, name)
                self.assertTrue(value.strip(), f"{row['id']} has an empty {name}")

    def test_prices_are_gmc_money_strings(self):
        for row in self.rows:
            self.assertRegex(row["price"], r"^\d+\.\d{2} USD$")

    def test_one_row_per_product_the_merchant_sells_and_no_more(self):
        self.assertGreaterEqual(len(self.rows), 100)
        self.assertEqual(len(self.rows), len(synthetic_catalog()))
        self.assertEqual(len({r["id"] for r in self.rows}), len(self.rows))

    def test_variants_share_their_products_group(self):
        groups = {}
        for row in self.rows:
            groups.setdefault(row["item_group_id"], []).append(row["id"])
        self.assertEqual(len(groups), 25)
        for gid, ids in groups.items():
            self.assertEqual(len(ids), 4, gid)
            self.assertTrue(all(i.startswith(gid + "-") for i in ids))

    def test_every_row_is_a_sku_the_checkout_will_offer(self):
        merchant = FakeMerchant(FakeFx("84.10"))
        for row in self.rows:
            terms = merchant.offer({row["id"]: 1}, "INR", NOW)
            self.assertEqual(terms.items[0].sku, row["id"])
            self.assertEqual(catalog_gmc.gmc_price(terms.items[0].unit_price, "USD"), row["price"])

    def test_links_point_at_the_merchants_own_pages(self):
        row = catalog_gmc.catalog(BASE + "/")[0]
        self.assertEqual(row["link"], f"{BASE}/p/{row['id']}")
        self.assertEqual(row["image_link"], f"{BASE}/img/{row['id']}.svg")
        with self.assertRaises(ValueError):
            catalog_gmc.catalog("shop.example")

    def test_availability_follows_stock(self):
        product = replace(synthetic_catalog()[0], stock=0)
        self.assertEqual(catalog_gmc.to_gmc(product, base_url=BASE)["availability"], "out_of_stock")
        self.assertEqual(self.rows[0]["availability"], "in_stock")

    def test_rows_are_deterministic(self):
        self.assertEqual(catalog_gmc.catalog(BASE), self.rows)

    def test_extended_fields_only_on_request(self):
        row = catalog_gmc.catalog(BASE, extended=True)[0]
        self.assertEqual(set(row), set(catalog_gmc.DOCUMENTED_FIELDS) | set(catalog_gmc.EXTENDED_FIELDS))
        self.assertEqual(row["identifier_exists"], "no")
        self.assertEqual(row["condition"], "new")

    def test_money_follows_the_currencys_minor_unit(self):
        self.assertEqual(catalog_gmc.gmc_price(Decimal("1999"), "JPY"), "1999 JPY")
        # Half to even, as Airwallex rounds (C9, 2026-10-29): a tie goes to the even cent.
        self.assertEqual(catalog_gmc.gmc_price(Decimal("51.985"), "USD"), "51.98 USD")
        self.assertEqual(catalog_gmc.gmc_price(Decimal("51.975"), "USD"), "51.98 USD")

    def test_a_sku_without_a_variant_suffix_is_refused(self):
        with self.assertRaises(ValueError):
            catalog_gmc.group_id("PLAIN")


class ChunksTest(unittest.TestCase):
    def setUp(self):
        self.rows = catalog_gmc.catalog(BASE)

    def test_the_whole_catalogue_fits_one_call(self):
        self.assertEqual([len(c) for c in catalog_gmc.chunks(self.rows)], [100])

    def test_a_product_is_never_split_across_calls(self):
        for limit in (4, 6, 7, 8, 299):
            calls = catalog_gmc.chunks(self.rows, limit)
            self.assertEqual(sum(len(c) for c in calls), len(self.rows))
            seen = {}
            for n, call in enumerate(calls):
                self.assertLessEqual(len(call), limit)
                for row in call:
                    seen.setdefault(row["item_group_id"], set()).add(n)
            self.assertTrue(all(len(where) == 1 for where in seen.values()), limit)

    def test_301_single_variant_rows_split_300_plus_1(self):
        rows = [{"id": f"X-{i:03d}-0", "item_group_id": f"X-{i:03d}"} for i in range(301)]
        self.assertEqual([len(c) for c in catalog_gmc.chunks(rows)], [300, 1])

    def test_a_product_with_more_variants_than_a_call_holds_is_refused(self):
        with self.assertRaises(ValueError):
            catalog_gmc.chunks(self.rows, 3)


class UploadTest(unittest.TestCase):
    def test_upsert_body_and_totals(self):
        sent = []

        def post(path, body):
            sent.append((path, body))
            return {"records_added": len(body["gmc_products"]), "records_updated": 0, "records_failed": 0,
                    "error_gcs_file_loc": ""}

        result = catalog_gmc.upload(catalog_gmc.catalog(BASE), post, limit=40, sleep=lambda s: None)
        self.assertTrue(result.ok)
        self.assertEqual(result.added, 100)
        self.assertEqual(len(sent), 3)  # 40, 40, 20 rows: ten, ten and five whole products
        for path, body in sent:
            self.assertEqual(path, "/pa/agentic_commerce/products")
            self.assertEqual(body["type"], "gmc")
            self.assertTrue(body["gmc_products"])
            self.assertNotIn("ingestion_mode", body)

    def test_failed_rows_are_counted_not_hidden(self):
        result = catalog_gmc.upload(catalog_gmc.catalog(BASE),
                                    lambda path, body: {"records_added": 98, "records_failed": 2},
                                    sleep=lambda s: None)
        self.assertFalse(result.ok)
        self.assertEqual((result.added, result.failed), (98, 2))

    def test_calls_are_spaced_out(self):
        pauses = []
        catalog_gmc.upload(catalog_gmc.catalog(BASE), lambda p, b: {}, limit=40, pause=0.2, sleep=pauses.append)
        self.assertEqual(pauses, [0.2, 0.2])


class LookupTest(unittest.TestCase):
    def test_a_live_catalogue_is_read_live(self):
        merchant = FakeMerchant(FakeFx("84.10"))
        sku = next(iter(merchant.catalog))
        find = catalog_gmc.lookup(merchant)
        merchant.set_price(sku, "9.99")
        self.assertEqual(find(sku).price, Decimal("9.99"))
        self.assertIsNone(find("NOPE-000-0"))

    def test_a_merchant_without_one_falls_back_to_the_published_catalogue(self):
        find = catalog_gmc.lookup(object())
        first = synthetic_catalog()[0]
        self.assertEqual(find(first.sku), first)


if __name__ == "__main__":
    unittest.main()
