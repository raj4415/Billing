"""Tiny HTTP server (standard library only): routing, handlers, security checks."""
import base64
import hmac
import mimetypes
import re
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from . import pdf, services as S, views as V

STATIC = Path(__file__).parent / "static"
MAX_BODY = 2 * 1024 * 1024
LOOPBACK = {"localhost", "127.0.0.1", "[::1]"}
ROUTES = []


def route(method, pattern):
    rx = re.compile("^" + pattern + "$")

    def deco(fn):
        ROUTES.append((method, rx, fn))
        return fn
    return deco


class Response:
    def __init__(self, body=b"", status=200, ctype="text/html; charset=utf-8", headers=None):
        self.body = body.encode("utf-8") if isinstance(body, str) else body
        self.status, self.ctype, self.headers = status, ctype, headers or {}


def redirect(url, msg=None, kind="ok"):
    if msg:
        url += ("&" if "?" in url else "?") + urlencode({"msg": msg, "kind": kind})
    return Response(status=303, headers={"Location": url})


class Ctx:
    """Per-request context handed to every route handler."""

    def __init__(self, conn, query, form, args):
        self.conn, self.query, self.form, self.args = conn, query, form, args
        self.cfg = S.get_settings(conn)

    def q(self, key, default=""):
        vals = self.query.get(key)
        return vals[0] if vals else default

    def page(self, title, body, active="/", status=200):
        flash = None
        if self.q("msg"):
            flash = (self.q("msg"), self.q("kind", "ok"))
        return Response(V.layout(title, body, self.cfg, active, flash), status)

    def fail(self):
        self.conn.rollback()


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
@route("GET", r"/")
def dashboard(ctx):
    return ctx.page("Dashboard", V.dashboard(S.dashboard(ctx.conn), ctx.cfg), "/")


# --------------------------------------------------------------------------
# Customers
# --------------------------------------------------------------------------
@route("GET", r"/customers")
def customers(ctx):
    q = ctx.q("q")
    return ctx.page("Customers", V.customers_list(S.list_customers(ctx.conn, q), q, ctx.cfg), "/customers")


@route("GET", r"/customers/new")
def customer_new(ctx):
    return ctx.page("New customer", V.customer_form({}), "/customers")


@route("GET", r"/customers/(\d+)/edit")
def customer_edit(ctx):
    cid = int(ctx.args[0])
    return ctx.page("Edit customer", V.customer_form(S.get_customer(ctx.conn, cid), cid), "/customers")


@route("POST", r"/customers/save")
def customer_save(ctx):
    cid = int(ctx.form.get("id") or 0) or None
    try:
        S.save_customer(ctx.conn, cid, ctx.form)
    except ValueError as exc:
        ctx.fail()
        values = {k: ctx.form.get(k) for k in ("name", "email", "phone", "address", "tax_id")}
        return ctx.page("Customer", V.customer_form(values, cid, str(exc)), "/customers", 400)
    return redirect("/customers", "Customer saved.")


@route("POST", r"/customers/(\d+)/delete")
def customer_delete(ctx):
    try:
        S.delete_customer(ctx.conn, int(ctx.args[0]))
    except ValueError as exc:
        ctx.fail()
        return redirect("/customers/%s/edit" % ctx.args[0], str(exc), "err")
    return redirect("/customers", "Customer deleted.")


# --------------------------------------------------------------------------
# Products
# --------------------------------------------------------------------------
@route("GET", r"/products")
def products(ctx):
    q = ctx.q("q")
    return ctx.page("Products", V.products_list(S.list_products(ctx.conn, q), q, ctx.cfg), "/products")


@route("GET", r"/products/new")
def product_new(ctx):
    return ctx.page("New item", V.product_form({"tax_rate": ctx.cfg["default_tax_rate"], "price": "0.00"}, ctx.cfg), "/products")


@route("GET", r"/products/(\d+)/edit")
def product_edit(ctx):
    pid = int(ctx.args[0])
    p = S.get_product(ctx.conn, pid)
    values = {"name": p["name"], "sku": p["sku"], "tax_rate": p["tax_rate"],
              "price": S.cents_to_str(p["unit_price_cents"])}
    return ctx.page("Edit item", V.product_form(values, ctx.cfg, pid), "/products")


@route("POST", r"/products/save")
def product_save(ctx):
    pid = int(ctx.form.get("id") or 0) or None
    try:
        S.save_product(ctx.conn, pid, ctx.form)
    except ValueError as exc:
        ctx.fail()
        values = {k: ctx.form.get(k) for k in ("name", "sku", "price", "tax_rate")}
        return ctx.page("Item", V.product_form(values, ctx.cfg, pid, str(exc)), "/products", 400)
    return redirect("/products", "Item saved.")


@route("POST", r"/products/(\d+)/delete")
def product_delete(ctx):
    S.delete_product(ctx.conn, int(ctx.args[0]))
    return redirect("/products", "Item deleted.")


# --------------------------------------------------------------------------
# Invoices
# --------------------------------------------------------------------------
def _invoice_form_page(ctx, draft, inv=None, error=None, status=200):
    body = V.invoice_form(draft, S.list_customers(ctx.conn), S.list_products(ctx.conn), ctx.cfg, inv, error)
    return ctx.page("Invoice", body, "/invoices", status)


@route("GET", r"/invoices")
def invoices(ctx):
    filters = {"q": ctx.q("q"), "status": ctx.q("status"), "customer_id": ctx.q("customer_id")}
    rows = S.list_invoices(ctx.conn, filters["status"], filters["q"], filters["customer_id"] or None)
    return ctx.page("Invoices", V.invoices_list(rows, filters, S.list_customers(ctx.conn), ctx.cfg), "/invoices")


@route("GET", r"/invoices/new")
def invoice_new(ctx):
    return _invoice_form_page(ctx, S.blank_draft(ctx.conn, ctx.q("customer_id")))


@route("GET", r"/invoices/(\d+)")
def invoice_view(ctx):
    inv = S.get_invoice(ctx.conn, int(ctx.args[0]))
    return ctx.page("Invoice " + inv["number"], V.invoice_detail(inv, ctx.cfg), "/invoices")


@route("GET", r"/invoices/(\d+)/edit")
def invoice_edit(ctx):
    inv = S.get_invoice(ctx.conn, int(ctx.args[0]))
    if inv["status"] == "void":
        return redirect("/invoices/%d" % inv["id"], "Void invoices cannot be edited.", "err")
    return _invoice_form_page(ctx, S.invoice_to_draft(inv), inv)


@route("POST", r"/invoices/save")
def invoice_save(ctx):
    inv_id = int(ctx.form.get("id") or 0) or None
    draft = S.draft_from_form(ctx.form)
    try:
        saved = S.save_invoice(ctx.conn, inv_id, draft, ctx.form.get("action", "draft"))
    except ValueError as exc:
        ctx.fail()
        inv = S.get_invoice(ctx.conn, inv_id) if inv_id else None
        return _invoice_form_page(ctx, draft, inv, str(exc), 400)
    return redirect("/invoices/%d" % saved, "Invoice saved.")


@route("POST", r"/invoices/(\d+)/(issue|void|delete)")
def invoice_action(ctx):
    inv_id, action = int(ctx.args[0]), ctx.args[1]
    try:
        if action == "issue":
            S.issue_invoice(ctx.conn, inv_id)
            return redirect("/invoices/%d" % inv_id, "Invoice issued.")
        if action == "void":
            S.void_invoice(ctx.conn, inv_id)
            return redirect("/invoices/%d" % inv_id, "Invoice voided.")
        S.delete_invoice(ctx.conn, inv_id)
        return redirect("/invoices", "Draft deleted.")
    except ValueError as exc:
        ctx.fail()
        return redirect("/invoices/%d" % inv_id, str(exc), "err")


@route("POST", r"/invoices/(\d+)/payments")
def payment_add(ctx):
    inv_id = int(ctx.args[0])
    try:
        S.add_payment(ctx.conn, inv_id, ctx.form)
    except ValueError as exc:
        ctx.fail()
        return redirect("/invoices/%d" % inv_id, str(exc), "err")
    return redirect("/invoices/%d" % inv_id, "Payment recorded.")


@route("POST", r"/invoices/(\d+)/payments/(\d+)/delete")
def payment_delete(ctx):
    inv_id = int(ctx.args[0])
    S.delete_payment(ctx.conn, inv_id, int(ctx.args[1]))
    return redirect("/invoices/%d" % inv_id, "Payment deleted.")


@route("GET", r"/invoices/(\d+)/pdf")
def invoice_pdf(ctx):
    inv = S.get_invoice(ctx.conn, int(ctx.args[0]))
    filename = re.sub(r"[^A-Za-z0-9._-]+", "_", inv["number"]) + ".pdf"
    return Response(pdf.build_invoice_pdf(inv, ctx.cfg), ctype="application/pdf",
                    headers={"Content-Disposition": 'inline; filename="%s"' % filename})


# --------------------------------------------------------------------------
# Reports, settings, backup
# --------------------------------------------------------------------------
@route("GET", r"/reports")
def reports(ctx):
    return ctx.page("Reports", V.reports_page(S.monthly_sales(ctx.conn), S.outstanding_by_customer(ctx.conn), ctx.cfg), "/reports")


@route("GET", r"/reports/invoices\.csv")
def reports_csv(ctx):
    return Response(S.invoices_csv(ctx.conn), ctype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="invoices.csv"'})


@route("GET", r"/settings")
def settings(ctx):
    return ctx.page("Settings", V.settings_page(ctx.cfg), "/settings")


@route("POST", r"/settings")
def settings_save(ctx):
    try:
        S.save_settings(ctx.conn, ctx.form)
    except ValueError as exc:
        ctx.fail()
        values = {k: ctx.form.get(k) for k in S.DEFAULT_SETTINGS}
        return ctx.page("Settings", V.settings_page(values, str(exc)), "/settings", 400)
    return redirect("/settings", "Settings saved.")


@route("GET", r"/backup")
def backup(ctx):
    return Response(ctx.server.db.backup_bytes(), ctype="application/octet-stream",
                    headers={"Content-Disposition": 'attachment; filename="billing-backup-%s.db"' % S.today()})


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------
def _host_only(host):
    if host.startswith("["):
        return host[: host.find("]") + 1]
    return host.split(":")[0]


class Handler(BaseHTTPRequestHandler):
    server_version = "BillingApp/1.0"

    def log_message(self, fmt, *args):
        if self.server.verbose:
            super().log_message(fmt, *args)

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def send(self, resp):
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.ctype)
        self.send_header("Content-Length", str(len(resp.body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        for key, val in resp.headers.items():
            self.send_header(key, val)
        self.end_headers()
        self.wfile.write(resp.body)

    def _guard(self, method):
        """Reject DNS-rebinding, cross-site POSTs and missing credentials."""
        host = self.headers.get("Host", "")
        if self.server.loopback_only and _host_only(host) not in LOOPBACK:
            self.send(Response("Forbidden host", 403, "text/plain"))
            return False
        origin = self.headers.get("Origin")
        if method == "POST" and origin and urlparse(origin).netloc != host:
            self.send(Response("Cross-site request blocked", 403, "text/plain"))
            return False
        if self.server.password:
            sent = self.headers.get("Authorization", "")
            expected = "Basic " + base64.b64encode(b"admin:" + self.server.password.encode()).decode()
            if not hmac.compare_digest(sent.encode(), expected.encode()):
                self.send(Response("Login required", 401, "text/plain",
                                   {"WWW-Authenticate": 'Basic realm="Billing", charset="UTF-8"'}))
                return False
        return True

    def _static(self, name):
        target = (STATIC / name).resolve()
        if STATIC.resolve() not in target.parents or not target.is_file():
            return None
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return Response(target.read_bytes(), ctype=ctype + "; charset=utf-8" if ctype.startswith("text") or "javascript" in ctype else ctype,
                        headers={"Cache-Control": "max-age=300"})

    def dispatch(self, method):
        try:
            if not self._guard(method):
                return
            url = urlparse(self.path)
            if method == "GET" and url.path.startswith("/static/"):
                resp = self._static(url.path[len("/static/"):])
                return self.send(resp or Response("Not found", 404, "text/plain"))
            body = b""
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    return self.send(Response("Request too large", 413, "text/plain"))
                body = self.rfile.read(length)
            form = S.Form(parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True))
            query = parse_qs(url.query, keep_blank_values=True)
            for m, rx, fn in ROUTES:
                match = rx.match(url.path) if m == method else None
                if match:
                    with self.server.db.session() as conn:
                        ctx = Ctx(conn, query, form, match.groups())
                        ctx.server = self.server
                        resp = fn(ctx)
                    return self.send(resp)
            self.send(self._error(404, "Page not found."))
        except LookupError as exc:
            self.send(self._error(404, str(exc)))
        except Exception:
            traceback.print_exc()
            self.send(self._error(500, "Something went wrong. Check the console for details."))

    def _error(self, code, message):
        with self.server.db.session() as conn:
            cfg = S.get_settings(conn)
        return Response(V.error_page(code, message, cfg), code)


class BillingServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, db, password=None, verbose=False):
        super().__init__(address, Handler)
        self.db, self.password, self.verbose = db, password, verbose
        self.loopback_only = address[0] in ("127.0.0.1", "localhost", "::1")


def make_server(db, host="127.0.0.1", port=8000, password=None, verbose=False):
    return BillingServer((host, port), db, password, verbose)
