import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from billing import services as S  # noqa: E402
from billing.db import Database  # noqa: E402
from billing.money import compute_totals, fmt_money, to_cents  # noqa: E402
from billing.web import make_server  # noqa: E402


def item(qty, price_cents, rate):
    return {"quantity": Decimal(qty), "unit_price_cents": price_cents, "tax_rate": Decimal(rate)}


class MoneyTests(unittest.TestCase):
    def test_to_cents_rounds_half_up(self):
        self.assertEqual(to_cents("10.005"), 1001)
        self.assertEqual(to_cents("1,234.50"), 123450)
        with self.assertRaises(ValueError):
            to_cents("abc")

    def test_format(self):
        self.assertEqual(fmt_money(123456789, "$"), "$1,234,567.89")
        self.assertEqual(fmt_money(-5, "$"), "-$0.05")

    def test_totals_with_tax(self):
        t = compute_totals([item("2", 5000, "10"), item("1", 1000, "0")])
        self.assertEqual((t["subtotal"], t["tax"], t["total"]), (11000, 1000, 12000))

    def test_percent_discount_applies_before_tax(self):
        t = compute_totals([item("1", 10000, "10")], "percent", Decimal(10))
        self.assertEqual((t["discount"], t["tax"], t["total"]), (1000, 900, 9900))

    def test_fixed_discount_across_rates(self):
        t = compute_totals([item("1", 10000, "10"), item("1", 10000, "0")], "fixed", Decimal(50))
        self.assertEqual(t["discount"], 5000)
        self.assertEqual(t["tax"], 750)  # 10% of 10000 * (15000/20000)
        self.assertEqual(t["total"], 15750)

    def test_fractional_quantity(self):
        t = compute_totals([item("1.5", 9000, "0")])
        self.assertEqual(t["total"], 13500)

    def test_empty(self):
        self.assertEqual(compute_totals([])["total"], 0)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.tmp.cleanup()

    def make_invoice(self, conn, action="issue", price="100", qty="2", tax="10", **over):
        cid = S.save_customer(conn, None, S.Form({"name": "Acme"}))
        d = S.blank_draft(conn, cid)
        d["items"] = [{"description": "Work", "quantity": qty, "unit_price": price, "tax_rate": tax}]
        d.update(over)
        return S.save_invoice(conn, None, d, action), cid

    def test_gapless_numbering_and_drafts(self):
        with self.db.session() as conn:
            draft, _ = self.make_invoice(conn, "draft")
            self.assertTrue(S.get_invoice(conn, draft)["number"].startswith("DRAFT-"))
            a, _ = self.make_invoice(conn)
            b, _ = self.make_invoice(conn)
            self.assertEqual(S.get_invoice(conn, a)["number"], "INV-0001")
            self.assertEqual(S.get_invoice(conn, b)["number"], "INV-0002")
            S.issue_invoice(conn, draft)
            self.assertEqual(S.get_invoice(conn, draft)["number"], "INV-0003")

    def test_totals_saved(self):
        with self.db.session() as conn:
            inv_id, _ = self.make_invoice(conn)
            inv = S.get_invoice(conn, inv_id)
            self.assertEqual((inv["subtotal_cents"], inv["tax_cents"], inv["total_cents"]), (20000, 2000, 22000))

    def test_payment_flow_and_status(self):
        with self.db.session() as conn:
            inv_id, _ = self.make_invoice(conn)
            self.assertEqual(S.get_invoice(conn, inv_id)["display_status"], "unpaid")
            S.add_payment(conn, inv_id, S.Form({"paid_on": S.today(), "amount": "100"}))
            self.assertEqual(S.get_invoice(conn, inv_id)["display_status"], "partial")
            with self.assertRaises(ValueError):
                S.add_payment(conn, inv_id, S.Form({"paid_on": S.today(), "amount": "9999"}))
            with self.assertRaises(ValueError):
                S.void_invoice(conn, inv_id)  # has payments
            S.add_payment(conn, inv_id, S.Form({"paid_on": S.today(), "amount": "120"}))
            inv = S.get_invoice(conn, inv_id)
            self.assertEqual((inv["display_status"], inv["balance_cents"]), ("paid", 0))

    def test_overdue(self):
        past = (date.today() - timedelta(days=40)).isoformat()
        with self.db.session() as conn:
            inv_id, _ = self.make_invoice(conn, issue_date=past, due_date=past)
            self.assertEqual(S.get_invoice(conn, inv_id)["display_status"], "overdue")
            self.assertEqual(S.dashboard(conn)["overdue_count"], 1)

    def test_validation(self):
        with self.db.session() as conn:
            cid = S.save_customer(conn, None, S.Form({"name": "X"}))
            base = S.blank_draft(conn, cid)
            for change, msg in [
                ({"customer_id": ""}, "customer"),
                ({"due_date": "2000-01-01"}, "Due date"),
                ({"items": []}, "line item"),
                ({"items": [{"description": "", "quantity": "1", "unit_price": "5", "tax_rate": "0"}]}, "description"),
                ({"items": [{"description": "a", "quantity": "0", "unit_price": "5", "tax_rate": "0"}]}, "quantity"),
                ({"items": [{"description": "a", "quantity": "1", "unit_price": "-5", "tax_rate": "0"}]}, "negative"),
                ({"items": [{"description": "a", "quantity": "1", "unit_price": "5", "tax_rate": "150"}]}, "tax rate"),
                ({"discount_type": "percent", "discount_value": "101"}, "exceed"),
            ]:
                with self.assertRaises(ValueError, msg=msg):
                    S.save_invoice(conn, None, dict(base, **change))

    def test_edit_void_delete_rules(self):
        with self.db.session() as conn:
            inv_id, cid = self.make_invoice(conn)
            with self.assertRaises(ValueError):
                S.delete_invoice(conn, inv_id)  # issued
            with self.assertRaises(ValueError):
                S.delete_customer(conn, cid)  # has invoices
            S.void_invoice(conn, inv_id)
            d = S.invoice_to_draft(S.get_invoice(conn, inv_id))
            with self.assertRaises(ValueError):
                S.save_invoice(conn, inv_id, d)  # void
            draft_id, _ = self.make_invoice(conn, "draft")
            S.delete_invoice(conn, draft_id)
            with self.assertRaises(LookupError):
                S.get_invoice(conn, draft_id)

    def test_cannot_lower_total_below_payments(self):
        with self.db.session() as conn:
            inv_id, _ = self.make_invoice(conn)
            S.add_payment(conn, inv_id, S.Form({"paid_on": S.today(), "amount": "200"}))
            d = S.invoice_to_draft(S.get_invoice(conn, inv_id))
            d["items"][0]["unit_price"] = "10"
            with self.assertRaises(ValueError):
                S.save_invoice(conn, inv_id, d)

    def test_settings_validation(self):
        with self.db.session() as conn:
            with self.assertRaises(ValueError):
                S.save_settings(conn, S.Form({"business_name": "", "currency": "$"}))
            S.save_settings(conn, S.Form({**S.DEFAULT_SETTINGS, "invoice_prefix": "A-", "next_invoice_number": "50"}))
            inv_id, _ = self.make_invoice(conn)
            self.assertEqual(S.get_invoice(conn, inv_id)["number"], "A-0050")

    def test_reports_and_csv(self):
        with self.db.session() as conn:
            S.seed_demo(conn)
            self.assertTrue(S.monthly_sales(conn))
            self.assertTrue(S.outstanding_by_customer(conn))
            lines = S.invoices_csv(conn).strip().splitlines()
            self.assertGreater(len(lines), 4)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Database(Path(cls.tmp.name) / "h.db")
        with cls.db.session() as conn:
            S.seed_demo(conn)
        cls.server = make_server(cls.db, "127.0.0.1", 0)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def request(self, path, data=None, headers=None, follow=True):
        body = urllib.parse.urlencode(data, doseq=True).encode() if data is not None else None
        req = urllib.request.Request(self.base + path, data=body, headers=headers or {})
        if not follow:
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *a, **k):
                    return None
            opener = urllib.request.build_opener(NoRedirect)
        else:
            opener = urllib.request.build_opener()
        try:
            r = opener.open(req)
            return r.status, r.read(), r.headers
        except urllib.error.HTTPError as err:
            return err.code, err.read(), err.headers

    def test_pages_render(self):
        for path in ["/", "/customers", "/customers/new", "/customers/1/edit", "/products", "/products/new",
                     "/products/1/edit", "/invoices", "/invoices?status=overdue", "/invoices/new",
                     "/invoices/1", "/invoices/1/edit", "/reports", "/settings", "/static/style.css", "/static/app.js"]:
            status, body, _ = self.request(path)
            self.assertEqual(status, 200, path)
            self.assertTrue(body, path)

    def test_404(self):
        self.assertEqual(self.request("/nope")[0], 404)
        self.assertEqual(self.request("/invoices/9999")[0], 404)
        self.assertEqual(self.request("/static/../main.py")[0], 404)

    def test_pdf_and_csv_and_backup(self):
        status, body, headers = self.request("/invoices/1/pdf")
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"%PDF"))
        self.assertEqual(headers["Content-Type"], "application/pdf")
        status, body, _ = self.request("/reports/invoices.csv")
        self.assertIn(b"Number,Customer", body)
        status, body, _ = self.request("/backup")
        self.assertTrue(body.startswith(b"SQLite format 3"))

    def test_create_invoice_via_form_and_pay(self):
        status, _, headers = self.request("/invoices/save", {
            "id": "", "customer_id": "1", "issue_date": S.today(), "due_date": S.today(),
            "discount_type": "none", "discount_value": "0", "notes": "<b>hi</b>", "action": "issue",
            "item_desc": ["Thing A", "Thing B"], "item_qty": ["2", "1"],
            "item_price": ["10.00", "5.50"], "item_tax": ["10", "0"],
        }, follow=False)
        self.assertEqual(status, 303)
        location = headers["Location"]
        status, page, _ = self.request(location.split("?")[0])
        self.assertIn(b"$27.50", page)  # 20 + 2 tax + 5.50
        self.assertIn(b"&lt;b&gt;hi&lt;/b&gt;", page)  # escaped
        self.assertNotIn(b"<b>hi</b>", page)
        inv_id = location.split("?")[0].rsplit("/", 1)[1]
        status, _, headers = self.request("/invoices/%s/payments" % inv_id,
                                          {"paid_on": S.today(), "amount": "27.50", "method": "Cash"}, follow=False)
        self.assertEqual(status, 303)
        self.assertIn(b"paid", self.request("/invoices/" + inv_id)[1])

    def test_form_errors_rerender(self):
        status, page, _ = self.request("/invoices/save", {
            "id": "", "customer_id": "", "issue_date": S.today(), "due_date": S.today(),
            "item_desc": ["Keep me"], "item_qty": ["1"], "item_price": ["1"], "item_tax": ["0"], "action": "issue"})
        self.assertEqual(status, 400)
        self.assertIn(b"choose a customer", page)
        self.assertIn(b"Keep me", page)
        status, page, _ = self.request("/customers/save", {"id": "", "name": ""})
        self.assertEqual(status, 400)

    def test_customer_crud(self):
        status, _, _ = self.request("/customers/save", {"id": "", "name": "Zed & Co", "email": "z@z.example"}, follow=False)
        self.assertEqual(status, 303)
        self.assertIn(b"Zed &amp; Co", self.request("/customers?q=Zed")[1])

    def test_cross_site_post_blocked(self):
        status, _, _ = self.request("/customers/save", {"name": "Evil"}, headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)

    def test_foreign_host_header_blocked(self):
        status, _, _ = self.request("/", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)


class PasswordTests(unittest.TestCase):
    def test_basic_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = make_server(Database(Path(tmp) / "p.db"), "127.0.0.1", 0, password="s3cret")
            threading.Thread(target=server.serve_forever, daemon=True).start()
            url = "http://127.0.0.1:%d/" % server.server_address[1]
            try:
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(url)
                self.assertEqual(ctx.exception.code, 401)
                import base64
                req = urllib.request.Request(url, headers={"Authorization": "Basic " + base64.b64encode(b"admin:s3cret").decode()})
                self.assertEqual(urllib.request.urlopen(req).status, 200)
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
