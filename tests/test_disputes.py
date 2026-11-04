"""U3: the dispute calls over a fake Airwallex, each as documented, and the receipt as evidence."""

import base64
import io
import json
import re
import unittest
import urllib.error
from datetime import datetime, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from countersign import disputes, receipt
from countersign.airwallex import AirwallexError

NOW = datetime(2026, 11, 30, 9, 0, tzinfo=timezone.utc)
MERCHANT = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
PUBLIC = base64.b64encode(MERCHANT.public_key().public_bytes(serialization.Encoding.Raw,
                                                             serialization.PublicFormat.Raw)).decode()
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

    def __init__(self, fail_on=None, error=None):
        self.posts = []
        self.fail_on, self.error = fail_on, error

    def token(self):
        return "token-SECRET"

    def request_id(self):
        return "req-1"

    def post(self, path, body=None, extra_headers=None):
        self.posts.append((path, body))
        if self.fail_on and self.fail_on in path:
            raise self.error
        status = {"create": "REQUIRES_RESPONSE", "challenge": "CHALLENGED", "resolve": "WON"}[path.rsplit("/", 1)[1]]
        return {"id": "dst_1", "stage": "CHARGEBACK", "status": status, "payment_intent_id": "int_paid",
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
                         ("int_paid", "CHARGEBACK", "10.4"))
        self.assertEqual(body["due_at"], "2026-12-07T09:00:00+0000")  # the guide's own format
        self.assertEqual((opened.id, opened.status), ("dst_1", "REQUIRES_RESPONSE"))

    def test_the_upload_goes_to_the_file_host_as_multipart(self):
        file_id = disputes.upload_file(self.api, b"%PDF-1.4 x", "receipt-ORD-1001.pdf", "dispute dst_1",
                                       opener=self.files)
        req = self.files.requests[0]
        self.assertEqual(file_id, "file_1")
        self.assertEqual(req.full_url, "https://files.sandbox.airwallex.com/api/v1/files/upload")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Authorization"), "Bearer token-SECRET")
        boundary = re.fullmatch(r"multipart/form-data; boundary=(\w+)", req.get_header("Content-type")).group(1)
        self.assertIn(b'name="file"; filename="receipt-ORD-1001.pdf"\r\nContent-Type: application/pdf\r\n\r\n'
                      b"%PDF-1.4 x\r\n", req.data)
        self.assertIn(b'name="notes"\r\n\r\ndispute dst_1\r\n', req.data)
        self.assertTrue(req.data.endswith(f"--{boundary}--\r\n".encode()))

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
        answered = disputes.submit_evidence(self.api, "dst_1", b"%PDF-1.4 x", filename="receipt-ORD-1001.pdf",
                                            description="d", product_description="1 x KIT-265-0", opener=self.files)
        path, body = self.api.posts[0]
        self.assertEqual(path, "/pa/payment_disputes/dst_1/challenge")
        self.assertEqual(body["request_id"], "req-1")
        self.assertEqual((body["challenge_method"], body["product_type"]), ("STANDARD", "PHYSICAL_GOODS"))
        self.assertEqual(body["supporting_documents"],
                         {"documents": [{"type": "PRIMARY", "file_ids": ["file_1"], "description": "d"}]})
        self.assertEqual(answered.status, "CHALLENGED")

    def test_evidence_over_visas_pdf_limit_is_never_uploaded(self):
        with self.assertRaises(ValueError):
            disputes.submit_evidence(self.api, "dst_1", b"x" * (disputes.MAX_PDF + 1), filename="r.pdf",
                                     description="d", product_description="p", opener=self.files)
        self.assertEqual((self.files.requests, self.api.posts), ([], []))

    def test_resolving_decides_for_the_merchant(self):
        resolved = disputes.resolve_for_merchant(self.api, "dst_1")
        self.assertEqual(self.api.posts, [("/simulation/pa/payment_disputes/dst_1/resolve", {"in_favor_of": "MERCHANT"})])
        self.assertEqual(resolved.status, "WON")


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


if __name__ == "__main__":
    unittest.main()
