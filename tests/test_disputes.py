"""U3: the dispute ending over a fake Airwallex: each call as documented, and the script stops when it should."""

import base64
import importlib.util
import io
import json
import pathlib
import re
import sys
import tempfile
import unittest
import unittest.mock
import os
import urllib.error
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from countersign import disputes, receipt
from countersign.airwallex import AirwallexError

_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "dispute_demo.py"
_spec = importlib.util.spec_from_file_location("dispute_demo", _PATH)
dispute_demo = importlib.util.module_from_spec(_spec)
sys.modules["dispute_demo"] = dispute_demo
_spec.loader.exec_module(dispute_demo)

NOW = datetime(2026, 11, 30, 9, 0, tzinfo=timezone.utc)
MERCHANT = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
PUBLIC = base64.b64encode(MERCHANT.public_key().public_bytes(serialization.Encoding.Raw,
                                                             serialization.PublicFormat.Raw)).decode()
SECRETS = {"AIRWALLEX_CLIENT_ID": "client-id-SECRET", "AIRWALLEX_API_KEY": "api-key-SECRET"}
SENTINEL = "VALUE-FROM-AIRWALLEX"  # in every fake answer; must never be printed


def signed_receipt(**charged):
    body = {
        "v": 1, "merchant_id": "m-1", "order_number": "ORD-1001", "payment_id": "int_paid", "idempotency_key": "k1",
        "agent": {"signature_agent": "https://agent.example", "key_id": "agent-key-1", "nonce": "n1",
                  "tag": "agent-payer-auth"},
        "approval": {"hash": "a" * 64, "ceiling": "8400.00", "currency": "INR", "approver": "shopper",
                     "approved_at": "2026-11-02T09:58:00+00:00", "expires_at": "2026-11-02T10:13:00+00:00"},
        "charged": {"amount": "8283.85", "currency": "INR", "merchant_amount": "98.50", "merchant_currency": "USD",
                    "fx_quote_id": "cvq_1", "rate": "84.10", **charged},
        "items": [{"sku": "KIT-265-0", "quantity": 1, "unit_price": "98.50"}],
        "delivery": "standard",
        "decided_at": "2026-11-02T10:00:00+00:00",
    }
    return receipt.sign(body, receipt.MerchantSigner(MERCHANT, "merchant-key-1"))


class FakeAirwallex:
    """The dispute simulation and challenge APIs as the reference answers them."""

    base_url = "https://api.sandbox.airwallex.com"
    timeout = 20

    def __init__(self, fail_on=None, error=None, decided="WON"):
        self.posts = []
        self.fail_on, self.error, self.decided = fail_on, error, decided
        self.stage = "RFI"

    def token(self):
        return "token-SECRET"

    def request_id(self):
        return "req-1"

    def post(self, path, body=None, extra_headers=None):
        self.posts.append((path, body))
        if self.fail_on and self.fail_on in path:
            raise self.error
        step = path.rsplit("/", 1)[1]
        if step == "create":
            self.stage = body["stage"]
        status = {"create": "REQUIRES_RESPONSE", "challenge": "CHALLENGED", "resolve": self.decided}[step]
        return {"id": "dst_1", "stage": self.stage, "status": status, "payment_intent_id": "int_paid",
                "amount": 98.5, "currency": "USD", "customer_name": SENTINEL, "issuer_comment": SENTINEL}


class FakeFiles:
    """Airwallex's file service: records each upload and hands back a file_id."""

    def __init__(self, error=None):
        self.requests = []
        self.error = error

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if self.error:
            raise self.error
        return io.BytesIO(json.dumps({"file_id": "file_1", "filename": SENTINEL, "size": len(req.data)}).encode())


class DisputeCallsTest(unittest.TestCase):
    def setUp(self):
        self.api, self.files = FakeAirwallex(), FakeFiles()

    def test_opening_sends_the_documented_fields_and_no_amount(self):
        opened = disputes.open_dispute(self.api, "int_paid", NOW)
        path, body = self.api.posts[0]
        self.assertEqual(path, "/simulation/pa/payment_disputes/create")
        self.assertEqual(set(body), {"payment_intent_id", "stage", "reason_code", "due_at"})
        self.assertEqual((body["payment_intent_id"], body["stage"], body["reason_code"]),
                         ("int_paid", "RFI", "10.4"))  # the builder guide's stage for a case to challenge
        self.assertEqual(body["due_at"], "2026-12-07T09:00:00+0000")  # the guide's own format
        self.assertEqual((opened.id, opened.stage, opened.status), ("dst_1", "RFI", "REQUIRES_RESPONSE"))

    def test_the_due_date_is_sent_in_utc_whatever_the_clock_says(self):
        ist = timezone(timedelta(hours=5, minutes=30))
        disputes.open_dispute(self.api, "int_paid", datetime(2026, 11, 30, 14, 30, tzinfo=ist))
        self.assertEqual(self.api.posts[0][1]["due_at"], "2026-12-07T09:00:00+0000")

    def test_the_upload_goes_to_the_file_host_as_multipart(self):
        file_id = disputes.upload_file(self.api, b"%PDF-1.4 x", "receipt-ORD-1001.pdf", "dispute dst_1",
                                       opener=self.files)
        req = self.files.requests[0]
        self.assertEqual(file_id, "file_1")
        self.assertEqual(req.full_url, "https://files.sandbox.airwallex.com/api/v1/files/upload")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Authorization"), "Bearer token-SECRET")
        self.assertEqual(req.get_header("User-agent"), "countersign/1.0")  # Cloudflare 1010 refuses Python-urllib
        boundary = re.fullmatch(r"multipart/form-data; boundary=(\w+)", req.get_header("Content-type")).group(1)
        self.assertIn(b'name="file"; filename="receipt-ORD-1001.pdf"\r\nContent-Type: application/pdf\r\n\r\n'
                      b"%PDF-1.4 x\r\n", req.data)
        self.assertIn(b'name="notes"\r\n\r\ndispute dst_1\r\n', req.data)
        self.assertTrue(req.data.endswith(f"--{boundary}--\r\n".encode()))

    def test_a_quote_or_line_break_cannot_end_a_multipart_header(self):
        disputes.upload_file(self.api, b"x", 'r"\r\nX-Injected: 1.pdf', 'n"\r\nx', opener=self.files)
        data = self.files.requests[0].data
        self.assertIn(b'filename="rX-Injected: 1.pdf"\r\n', data)
        self.assertIn(b'name="notes"\r\n\r\nnx\r\n', data)

    def test_the_file_host_follows_the_api_host(self):
        self.assertEqual(disputes.files_url("https://api.sandbox.airwallex.com"), "https://files.sandbox.airwallex.com")
        self.assertEqual(disputes.files_url("https://api.airwallex.com/"), "https://files.airwallex.com")
        with self.assertRaises(ValueError):
            disputes.files_url("https://example.com")

    def test_a_refused_upload_is_an_airwallex_error(self):
        refused = urllib.error.HTTPError("u", 400, "Bad Request", {}, io.BytesIO(b'{"code": "invalid_argument"}'))
        with self.assertRaises(AirwallexError) as err:
            disputes.upload_file(self.api, b"x", "r.pdf", opener=FakeFiles(refused))
        self.assertEqual((err.exception.status, err.exception.body["code"]), (400, "invalid_argument"))

    def test_the_challenge_names_the_receipt_as_the_primary_document(self):
        rfi = disputes.Dispute("dst_1", "RFI", "REQUIRES_RESPONSE", "int_paid")
        answered = disputes.submit_evidence(self.api, rfi, b"%PDF-1.4 x", filename="receipt-ORD-1001.pdf",
                                            description="d", product_description="1 x KIT-265-0", opener=self.files)
        path, body = self.api.posts[0]
        self.assertEqual(path, "/pa/payment_disputes/dst_1/challenge")
        self.assertEqual(body, {"request_id": "req-1", "challenge_method": "STANDARD", "reason": "OTHER_REASONS",
                                "supporting_documents": {"documents": [{"type": "PRIMARY", "file_ids": ["file_1"],
                                                                        "description": "d"}]}})
        self.assertEqual(answered.status, "CHALLENGED")

    def test_a_chargeback_challenge_also_says_what_was_sold(self):
        chargeback = disputes.Dispute("dst_1", "CHARGEBACK", "REQUIRES_RESPONSE", "int_paid")
        disputes.challenge(self.api, chargeback, "file_1", description="d", product_description="1 x KIT-265-0",
                           request_id="req-1")
        body = self.api.posts[0][1]
        self.assertEqual((body["product_type"], body["product_description"]), ("PHYSICAL_GOODS", "1 x KIT-265-0"))

    def test_evidence_over_visas_pdf_limit_is_never_uploaded(self):
        with self.assertRaises(ValueError):
            disputes.submit_evidence(self.api, disputes.Dispute("dst_1", "RFI", "", None),
                                     b"x" * (disputes.MAX_PDF + 1), filename="r.pdf",
                                     description="d", product_description="p", opener=self.files)
        self.assertEqual((self.files.requests, self.api.posts), ([], []))

    def test_resolving_decides_for_the_merchant(self):
        resolved = disputes.resolve_for_merchant(self.api, "dst_1")
        self.assertEqual(self.api.posts, [("/simulation/pa/payment_disputes/dst_1/resolve", {"in_favor_of": "MERCHANT"})])
        self.assertEqual(resolved.status, "WON")


class ChallengeDetailsTest(unittest.TestCase):
    """The signed order in the challenge's own fields, by stage, as Airwallex's reference lists them."""

    def setUp(self):
        self.api = FakeAirwallex()

    def sent(self, stage, details):
        disputes.challenge(self.api, disputes.Dispute("dst_1", stage, "REQUIRES_RESPONSE", "int_paid"), "file_1",
                           description="d", product_description="1 x KIT-265-0", request_id="req-1", details=details)
        return self.api.posts[-1][1]

    def test_without_details_a_chargeback_body_is_the_one_that_won_live(self):
        self.assertEqual(set(self.sent("CHARGEBACK", None)), {"request_id", "challenge_method", "reason",
                                                              "supporting_documents", "product_type",
                                                              "product_description"})

    def test_at_chargeback_the_receipt_says_what_was_sold_and_why_no_refund(self):
        details = disputes.challenge_details(signed_receipt(), "CHARGEBACK",
                                             verify_url="https://shop.example/verify")
        body = self.sent("CHARGEBACK", details)
        self.assertEqual(body["product_type"], "PHYSICAL_GOODS")
        self.assertEqual(body["product_description"], "1 x KIT-265-0 at 98.50 USD; order ORD-1001, charged 8283.85 INR")
        self.assertEqual(body["refund_refusal_reason"],
                         "The receipt the merchant signed for order ORD-1001 records this charge, 8283.85 INR "
                         "(ed25519 key merchant-key-1); check it at https://shop.example/verify.")
        for rfi_only in ("order_info", "seller_info", "delivery_info"):
            self.assertNotIn(rfi_only, body)

    def test_at_rfi_the_order_goes_field_by_field(self):
        details = disputes.challenge_details(signed_receipt(), "RFI", store_url="https://shop.example/")
        body = self.sent("RFI", details)
        self.assertEqual(body["order_info"], {
            "id": "ORD-1001", "created_at": "2026-11-02T10:00:00+00:00",
            "products": [{"sku": "KIT-265-0", "code": "KIT-265-0", "type": "physical_good", "quantity": 1,
                          "desc": "98.50 USD each; the order was charged in INR at 84.10 INR per USD",
                          "url": "https://shop.example/p/KIT-265-0"}],
            "total_amount": 8283.85, "total_currency": "INR"})
        self.assertEqual(body["seller_info"], {"store_url": "https://shop.example"})
        self.assertEqual(body["delivery_info"], {"shipping_method": "standard"})
        for chargeback_only in ("product_type", "product_description", "refund_refusal_reason"):
            self.assertNotIn(chargeback_only, body)
        json.dumps(body, allow_nan=False)  # what the client sends: amounts are plain numbers

    def test_a_unit_price_is_a_number_only_in_the_currency_charged(self):
        same = signed_receipt(amount="98.50", currency="USD")
        product = disputes.challenge_details(same, "RFI")["order_info"]["products"][0]
        self.assertEqual(product["unit_price"], 98.5)
        self.assertNotIn("desc", product)
        other = disputes.challenge_details(signed_receipt(), "RFI")["order_info"]["products"][0]
        self.assertNotIn("unit_price", other)  # 98.50 is USD; a bare number would read as INR

    def test_without_a_host_there_is_no_link_and_no_seller(self):
        details = disputes.challenge_details(signed_receipt(), "RFI")
        self.assertNotIn("url", details["order_info"]["products"][0])
        self.assertNotIn("seller_info", details)
        reason = disputes.challenge_details(signed_receipt(), "CHARGEBACK")["refund_refusal_reason"]
        self.assertNotIn("check it at", reason)

    def test_details_cannot_replace_the_evidence_or_the_request(self):
        body = self.sent("RFI", {"request_id": "x", "reason": "x", "challenge_method": "x",
                                 "supporting_documents": {"documents": []}, "customer_info": {"name": "x"},
                                 "seller_info": {"store_url": "https://shop.example"}})
        self.assertEqual((body["request_id"], body["reason"], body["challenge_method"]),
                         ("req-1", "OTHER_REASONS", "STANDARD"))
        self.assertEqual(body["supporting_documents"]["documents"][0]["file_ids"], ["file_1"])
        self.assertNotIn("customer_info", body)  # the merchant holds no customer details to send
        self.assertEqual(body["seller_info"], {"store_url": "https://shop.example"})

    def test_another_stage_gets_nothing_extra(self):
        self.assertEqual(disputes.challenge_details(signed_receipt(), "PRE_ARBITRATION"), {})

    def test_long_values_are_cut_to_airwallexs_limits(self):
        signed = signed_receipt()
        signed["body"]["items"] = [{"sku": "S" * 200, "quantity": 1, "unit_price": "1.00"}]
        product = disputes.challenge_details(signed, "RFI", store_url="https://shop.example")["order_info"]["products"][0]
        self.assertEqual(len(product["sku"]), disputes.MAX_SKU)
        self.assertTrue(product["url"].endswith("/p/" + "S" * disputes.MAX_SKU))
        signed["body"]["items"] = [{"sku": "S", "quantity": 1, "unit_price": "1.00"}] * 40
        details = disputes.challenge_details(signed, "CHARGEBACK")
        self.assertEqual(len(details["product_description"]), disputes.MAX_TEXT)

    def test_an_odd_sku_cannot_change_the_link(self):
        signed = signed_receipt()
        signed["body"]["items"] = [{"sku": "../admin?x=1", "quantity": 1, "unit_price": "1.00"}]
        product = disputes.challenge_details(signed, "RFI", store_url="https://shop.example")["order_info"]["products"][0]
        self.assertEqual(product["url"], "https://shop.example/p/..%2Fadmin%3Fx%3D1")


class EvidencePdfTest(unittest.TestCase):
    def test_the_pdf_is_well_formed_and_carries_the_receipt(self):
        signed = signed_receipt()
        check = receipt.verify(signed, MERCHANT.public_key())
        pdf = disputes.evidence_pdf(signed, check.findings, PUBLIC, "https://shop.example/verify")
        self.assertTrue(pdf.startswith(b"%PDF-1.4\n") and pdf.endswith(b"%%EOF\n"))
        self.assertLess(len(pdf), disputes.MAX_PDF)
        self.assert_xref_points_at_objects(pdf)
        for text in (b"(Order ORD-1001, payment int_paid, merchant m-1)", PUBLIC.encode(),
                     b"https://shop.example/verify", b"signed by the merchant's key"):
            self.assertIn(text, pdf)

    def test_the_json_in_the_pdf_is_the_signed_json_cut_never_reflowed(self):
        signed = signed_receipt()
        lines = disputes.evidence_lines(signed, (), PUBLIC)
        mono = [text for font, text in lines if font == "mono"]
        self.assertTrue(all(len(t) <= disputes.MONO_WIDTH for t in mono))
        self.assertEqual("".join(mono), "".join(json.dumps(signed, indent=2, sort_keys=True).splitlines()))
        self.assertIn(signed["signature"], "".join(mono))

    def test_a_long_receipt_runs_onto_more_pages(self):
        lines = [("mono", f"line {i}") for i in range(200)]
        pdf = disputes.render_pdf(lines)
        self.assertIn(b"/Count 3 ", pdf)
        self.assert_xref_points_at_objects(pdf)

    def test_brackets_and_backslashes_cannot_end_a_string_early(self):
        pdf = disputes.render_pdf([("text", r"a (b) c\d")])
        self.assertIn(rb"(a \(b\) c\\d) Tj", pdf)

    def assert_xref_points_at_objects(self, pdf):
        start = int(pdf.rsplit(b"startxref\n", 1)[1].split(b"\n")[0])
        self.assertTrue(pdf[start:].startswith(b"xref\n"))
        rows = pdf[start:].split(b"\n")
        count = int(rows[1].split()[1])
        for n in range(1, count):
            offset = int(rows[2 + n][:10])
            self.assertTrue(pdf[offset:].startswith(b"%d 0 obj\n" % n), n)


class DisputeDemoTest(unittest.TestCase):
    def setUp(self):
        self.api, self.files, self.lines = FakeAirwallex(), FakeFiles(), []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)

    def run_demo(self, signed, *argv, api=None, files=None, environ=None, key=PUBLIC):
        file = self.tmp / "receipt.json"
        file.write_text(json.dumps(signed))
        return dispute_demo.main(["--receipt", str(file), "--merchant-key", key, *argv], environ=environ or {},
                                 client=self.api if api is None else api, opener=files or self.files, now=NOW,
                                 out=self.lines.append)

    def paths(self, api=None):
        return [p.rsplit("/", 1)[1] for p, _ in (api or self.api).posts]

    def test_the_scene_opens_answers_and_wins(self):
        self.assertEqual(self.run_demo(signed_receipt()), 0)
        self.assertEqual(self.paths(), ["create", "challenge", "resolve"])
        self.assertEqual(len(self.files.requests), 1)
        self.assertIn(b"%PDF-1.4", self.files.requests[0].data)
        text = "\n".join(self.lines)
        for expected in ("dst_1  CHARGEBACK REQUIRES_RESPONSE", "VERIFIED", "within the approved 8400.00",
                         "CHARGEBACK CHALLENGED", "CHARGEBACK WON"):
            self.assertIn(expected, text)
        self.assertEqual(self.api.posts[0][1]["stage"], "CHARGEBACK")  # the sandbox decides only from here
        self.assertEqual(self.api.posts[1][1]["product_type"], "PHYSICAL_GOODS")
        self.assertIn("ORD-1001", self.api.posts[1][1]["refund_refusal_reason"])  # the order by default (won live 11-06)
        self.assertIn("plus product_description, refund_refusal_reason)", self.lines[-2])

    def test_the_flag_still_works_and_changes_nothing(self):
        self.run_demo(signed_receipt(), "--base-url", "https://shop.example")
        default = self.api.posts[1][1]
        self.api, self.lines = FakeAirwallex(), []
        self.run_demo(signed_receipt(), "--structured", "--base-url", "https://shop.example")
        self.assertEqual(self.api.posts[1][1], default)

    def test_the_stage_can_be_rfi(self):
        self.assertEqual(self.run_demo(signed_receipt(), "--stage", "RFI"), 0)
        self.assertEqual(self.api.posts[0][1]["stage"], "RFI")
        self.assertNotIn("product_type", self.api.posts[1][1])

    def test_no_structured_is_the_challenge_that_won_on_11_05(self):
        self.run_demo(signed_receipt(), "--no-structured", "--base-url", "https://shop.example")
        self.assertNotIn("refund_refusal_reason", self.api.posts[1][1])
        self.assertTrue(self.lines[-2].endswith("bytes)"))

    def test_structured_adds_the_chargeback_fields_and_says_which(self):
        self.assertEqual(self.run_demo(signed_receipt(), "--structured", "--base-url", "https://shop.example/"), 0)
        body = self.api.posts[1][1]
        self.assertIn("ORD-1001", body["refund_refusal_reason"])
        self.assertIn("check it at https://shop.example/verify", body["refund_refusal_reason"])
        self.assertIn("charged 8283.85 INR", body["product_description"])
        self.assertIn("plus product_description, refund_refusal_reason)", self.lines[-2])

    def test_structured_at_rfi_sends_the_order(self):
        self.assertEqual(self.run_demo(signed_receipt(), "--stage", "RFI", "--structured",
                                       "--base-url", "https://shop.example"), 0)
        body = self.api.posts[1][1]
        self.assertEqual(body["order_info"]["id"], "ORD-1001")
        self.assertEqual(body["order_info"]["products"][0]["url"], "https://shop.example/p/KIT-265-0")
        self.assertNotIn("product_type", body)
        self.assertIn("plus delivery_info, order_info, seller_info)", self.lines[-2])

    def test_a_dispute_not_decided_for_the_merchant_says_so(self):
        api = FakeAirwallex(decided="LOST")
        self.assertEqual(self.run_demo(signed_receipt(), api=api), 2)
        self.assertEqual(self.lines[-1], "  stopped: the dispute ended LOST, not for the merchant")

    def test_a_receipt_with_no_body_opens_nothing(self):
        for broken in ({"body": "x"}, ["not", "a", "receipt"]):
            with self.subTest(broken=broken):
                self.assertEqual(self.run_demo(broken), 1)
                self.assertEqual(self.api.posts, [])

    def test_a_tampered_receipt_stops_before_any_evidence(self):
        signed = signed_receipt()
        signed["body"]["charged"]["amount"] = "1.00"
        self.assertEqual(self.run_demo(signed), 1)
        self.assertEqual((self.paths(), self.files.requests), (["create"], []))
        self.assertIn("FAILED", "\n".join(self.lines))
        self.assertIn("dst_1 stays open", self.lines[-1])

    def test_a_receipt_signed_by_another_key_stops_before_any_evidence(self):
        other = Ed25519PrivateKey.generate()
        signed = receipt.sign(signed_receipt()["body"], receipt.MerchantSigner(other, "merchant-key-1"))
        self.assertEqual(self.run_demo(signed), 1)
        self.assertEqual((self.paths(), self.files.requests), (["create"], []))

    def test_a_receipt_for_another_payment_opens_nothing(self):
        self.assertEqual(self.run_demo(signed_receipt(), "--intent", "int_other"), 1)
        self.assertEqual(self.api.posts, [])
        self.assertIn("for payment int_paid, not int_other", self.lines[-1])

    def test_a_merchant_key_that_is_not_a_key_opens_nothing(self):
        for key in ("not-a-key", base64.b64encode(b"short").decode()):
            with self.subTest(key=key):
                self.assertEqual(self.run_demo(signed_receipt(), key=key), 1)
                self.assertEqual(self.api.posts, [])

    def test_the_key_comes_from_the_host_when_none_is_given(self):
        file = self.tmp / "receipt.json"
        file.write_text(json.dumps(signed_receipt()))
        asked = []
        code = dispute_demo.main(["--receipt", str(file)], environ={"COUNTERSIGN_PUBLIC_URL": "https://shop.example/"},
                                 client=self.api, fetch_key=lambda url: asked.append(url) or PUBLIC,
                                 opener=self.files, now=NOW, out=self.lines.append)
        self.assertEqual((code, asked), (0, ["https://shop.example/"]))
        self.assertIn(b"https://shop.example/verify", self.files.requests[0].data)

    def test_a_host_that_gives_no_key_opens_nothing(self):
        file = self.tmp / "receipt.json"
        file.write_text(json.dumps(signed_receipt()))
        for error in (TimeoutError("timed out"), KeyError("public_key"), TypeError("[] is not a dict"),
                      ValueError("unknown url type")):
            with self.subTest(error=type(error).__name__):
                def fetch(url):
                    raise error
                code = dispute_demo.main(["--receipt", str(file), "--base-url", "https://shop.example"], environ={},
                                         client=self.api, fetch_key=fetch, opener=self.files, now=NOW,
                                         out=self.lines.append)
                self.assertEqual((code, self.api.posts), (2, []))

    def test_a_5xx_from_the_simulation_api_stops_cleanly(self):
        for step, fail_on in (("open", "create"), ("evidence", "challenge"), ("resolve", "resolve")):
            with self.subTest(step=step):
                self.lines.clear()
                api = FakeAirwallex(fail_on, AirwallexError(503, {"code": "service_unavailable", "message": SENTINEL}))
                self.assertEqual(self.run_demo(signed_receipt(), api=api, files=FakeFiles()), 2)
                self.assertEqual(self.paths(api)[-1], fail_on)  # nothing after the refused call
                self.assertEqual(self.lines[-1], f"  stopped at {step}: Airwallex answered HTTP 503 service_unavailable")

    def test_an_upload_that_never_answers_stops_cleanly(self):
        self.assertEqual(self.run_demo(signed_receipt(), files=FakeFiles(TimeoutError("timed out"))), 2)
        self.assertEqual(self.paths(), ["create"])
        self.assertEqual(self.lines[-1], "  stopped at evidence: Airwallex could not be reached (TimeoutError)")

    def test_no_secret_header_or_airwallex_value_is_printed(self):
        # The real client from .env this time, its transport faked: login, then the three dispute calls.
        seen = []

        def urlopen(req, timeout=None):
            seen.append(req)
            if req.full_url.endswith("/authentication/login"):
                return io.BytesIO(json.dumps({"token": "token-SECRET", "expires_at": SENTINEL}).encode())
            answer = FakeAirwallex().post(req.full_url.split("/api/v1")[1], json.loads(req.data or b"{}"))
            return io.BytesIO(json.dumps(answer).encode())

        file = self.tmp / "receipt.json"
        file.write_text(json.dumps(signed_receipt()))
        with unittest.mock.patch("urllib.request.urlopen", urlopen), unittest.mock.patch.dict(os.environ, SECRETS):
            code = dispute_demo.main(["--receipt", str(file), "--merchant-key", PUBLIC], environ={},
                                     opener=self.files, now=NOW, out=self.lines.append)
        self.assertEqual(code, 0)
        self.assertEqual(seen[0].get_header("X-api-key"), "api-key-SECRET")  # it did log in with them
        self.assertEqual(self.files.requests[0].get_header("Authorization"), "Bearer token-SECRET")
        printed = "\n".join(self.lines)
        for secret in ("client-id-SECRET", "api-key-SECRET", "token-SECRET", SENTINEL, "Bearer"):
            self.assertNotIn(secret, printed)


if __name__ == "__main__":
    unittest.main()
