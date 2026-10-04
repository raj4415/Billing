"""HTML rendering. Every dynamic value goes through e() to prevent XSS."""
import html
import json

from .money import cents_to_str, fmt_money, fmt_num
from .services import PAYMENT_METHODS, STATUSES, today

NAV = [("/", "Dashboard"), ("/invoices", "Invoices"), ("/customers", "Customers"),
       ("/products", "Products"), ("/reports", "Reports"), ("/settings", "Settings")]


def e(value):
    return html.escape("" if value is None else str(value), quote=True)


def money(cents, cfg):
    return fmt_money(cents, cfg["currency"])


def badge(status):
    return '<span class="badge s-%s">%s</span>' % (e(status), e(status))


def layout(title, body, cfg, active="/", flash=None):
    links = []
    for href, text in NAV:
        cls = "active" if href == active else ""
        links.append('<a href="%s" class="%s">%s</a>' % (href, cls, text))
    flash_html = ""
    if flash:
        msg, kind = flash
        kind = kind if kind in ("ok", "err") else "ok"
        flash_html = '<div class="flash %s">%s</div>' % (kind, e(msg))
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>%s \u00b7 %s</title>"
        '<link rel="stylesheet" href="/static/style.css"></head>'
        '<body data-currency="%s"><header class="top"><div class="in">'
        '<a class="brand" href="/">%s</a><nav>%s</nav></div></header>'
        "<main>%s%s</main>"
        '<script src="/static/app.js"></script></body></html>'
    ) % (e(title), e(cfg["business_name"]), e(cfg["currency"]), e(cfg["business_name"]),
         "".join(links), flash_html, body)


def error_page(code, message, cfg):
    return layout("Error", '<div class="card"><h1>%s</h1><p>%s</p><a class="btn" href="/">Back to dashboard</a></div>'
                  % (e(code), e(message)), cfg)


def field(label, name, value="", type_="text", required=False, extra="", cls=""):
    return '<label class="%s">%s<input type="%s" name="%s" value="%s"%s %s></label>' % (
        cls, e(label), type_, name, e(value), " required" if required else "", extra)


def textarea(label, name, value="", rows=3, cls=""):
    return '<label class="%s">%s<textarea name="%s" rows="%d">%s</textarea></label>' % (
        cls, e(label), name, rows, e(value))


def confirm_form(action, label, message, cls="btn small danger"):
    return ('<form class="inline" method="post" action="%s" onsubmit="return confirm(\'%s\')">'
            '<button class="%s">%s</button></form>') % (action, e(message).replace("'", ""), cls, e(label))


# --------------------------------------------------------------------------
def dashboard(d, cfg):
    stats = [("Total invoiced", d["invoiced"]), ("Collected", d["collected"]),
             ("Outstanding", d["outstanding"]), ("Overdue", d["overdue"])]
    cards = "".join('<div class="stat"><div class="k">%s</div><div class="v">%s</div></div>'
                    % (k, e(money(v, cfg))) for k, v in stats)

    def rows(invoices, empty):
        if not invoices:
            return '<tr><td colspan="4" class="empty">%s</td></tr>' % empty
        return "".join(
            '<tr><td><a href="/invoices/%d">%s</a></td><td>%s</td><td class="r">%s</td><td>%s</td></tr>'
            % (i["id"], e(i["number"]), e(i["customer_name"]), e(money(i["balance_cents"] if i["status"] == "issued" else i["total_cents"], cfg)),
               badge(i["display_status"])) for i in invoices)

    head = "<thead><tr><th>Invoice</th><th>Customer</th><th class=\"r\">Amount</th><th>Status</th></tr></thead>"
    return (
        '<div class="bar"><h1>Dashboard</h1><div class="actions">'
        '<a class="btn primary" href="/invoices/new">New invoice</a></div></div>'
        '<div class="grid">%s</div>'
        '<div class="cols">'
        '<div class="card"><h2>Recent invoices</h2><div class="scroll"><table>%s<tbody>%s</tbody></table></div></div>'
        '<div class="card"><h2>Overdue (%d)</h2><div class="scroll"><table>%s<tbody>%s</tbody></table></div></div>'
        "</div>"
        '<p class="muted">%d customers \u00b7 %d draft invoices</p>'
    ) % (cards, head, rows(d["recent"], "No invoices yet."), d["overdue_count"], head,
         rows(d["overdue_list"], "Nothing overdue."), d["customer_count"], d["draft_count"])


# --------------------------------------------------------------------------
def customers_list(rows, q, cfg):
    body = "".join(
        '<tr><td><a href="/customers/%d/edit">%s</a></td><td>%s</td><td>%s</td><td class="r">%d</td>'
        '<td class="r">%s</td><td class="r"><a class="btn small" href="/invoices/new?customer_id=%d">Invoice</a></td></tr>'
        % (c["id"], e(c["name"]), e(c["email"]), e(c["phone"]), c["invoice_count"],
           e(money(c["balance_cents"], cfg)), c["id"]) for c in rows
    ) or '<tr><td colspan="6" class="empty">No customers found.</td></tr>'
    return (
        '<div class="bar"><h1>Customers</h1><div class="actions">'
        '<form method="get" action="/customers"><input name="q" value="%s" placeholder="Search\u2026"></form>'
        '<a class="btn primary" href="/customers/new">Add customer</a></div></div>'
        '<div class="card scroll"><table><thead><tr><th>Name</th><th>Email</th><th>Phone</th>'
        '<th class="r">Invoices</th><th class="r">Balance</th><th></th></tr></thead><tbody>%s</tbody></table></div>'
    ) % (e(q), body)


def customer_form(values, cid=None, error=None):
    err = '<div class="flash err">%s</div>' % e(error) if error else ""
    delete = confirm_form("/customers/%d/delete" % cid, "Delete", "Delete this customer?") if cid else ""
    return (
        '<div class="bar"><h1>%s</h1></div>%s<form class="card" method="post" action="/customers/save">'
        '<input type="hidden" name="id" value="%s"><div class="form-grid">%s%s%s%s%s</div>'
        '<p class="actions"><button class="primary">Save</button><a class="btn" href="/customers">Cancel</a></p></form>%s'
    ) % ("Edit customer" if cid else "New customer", err, cid or "",
         field("Name", "name", values.get("name"), required=True),
         field("Email", "email", values.get("email"), "email"),
         field("Phone", "phone", values.get("phone")),
         field("Tax ID", "tax_id", values.get("tax_id")),
         textarea("Address", "address", values.get("address"), cls="wide"), delete)


# --------------------------------------------------------------------------
def products_list(rows, q, cfg):
    body = "".join(
        '<tr><td><a href="/products/%d/edit">%s</a></td><td>%s</td><td class="r">%s</td><td class="r">%s%%</td></tr>'
        % (p["id"], e(p["name"]), e(p["sku"]), e(money(p["unit_price_cents"], cfg)), e(p["tax_rate"])) for p in rows
    ) or '<tr><td colspan="4" class="empty">No products or services yet.</td></tr>'
    return (
        '<div class="bar"><h1>Products &amp; services</h1><div class="actions">'
        '<form method="get" action="/products"><input name="q" value="%s" placeholder="Search\u2026"></form>'
        '<a class="btn primary" href="/products/new">Add item</a></div></div>'
        '<div class="card scroll"><table><thead><tr><th>Name</th><th>SKU</th><th class="r">Price</th>'
        '<th class="r">%s</th></tr></thead><tbody>%s</tbody></table></div>'
    ) % (e(q), e(cfg["tax_label"]), body)


def product_form(values, cfg, pid=None, error=None):
    err = '<div class="flash err">%s</div>' % e(error) if error else ""
    delete = confirm_form("/products/%d/delete" % pid, "Delete", "Delete this item?") if pid else ""
    return (
        '<div class="bar"><h1>%s</h1></div>%s<form class="card" method="post" action="/products/save">'
        '<input type="hidden" name="id" value="%s"><div class="form-grid">%s%s%s%s</div>'
        '<p class="actions"><button class="primary">Save</button><a class="btn" href="/products">Cancel</a></p></form>%s'
    ) % ("Edit item" if pid else "New product / service", err, pid or "",
         field("Name", "name", values.get("name"), required=True),
         field("SKU / code", "sku", values.get("sku")),
         field("Unit price (%s)" % cfg["currency"], "price", values.get("price"), extra='inputmode="decimal"', required=True),
         field("%s rate (%%)" % cfg["tax_label"], "tax_rate", values.get("tax_rate"), extra='inputmode="decimal"'), delete)


# --------------------------------------------------------------------------
def invoices_list(rows, filters, customers, cfg):
    status_opts = '<option value="">All</option>' + "".join(
        '<option value="%s"%s>%s</option>' % (s, " selected" if s == filters["status"] else "", s.title())
        for s in STATUSES)
    cust_opts = '<option value="">All customers</option>' + "".join(
        '<option value="%d"%s>%s</option>' % (c["id"], " selected" if str(c["id"]) == filters["customer_id"] else "", e(c["name"]))
        for c in customers)
    body = "".join(
        '<tr><td><a href="/invoices/%d">%s</a></td><td>%s</td><td>%s</td><td>%s</td><td class="r">%s</td>'
        '<td class="r">%s</td><td>%s</td></tr>'
        % (i["id"], e(i["number"]), e(i["customer_name"]), e(i["issue_date"]), e(i["due_date"]),
           e(money(i["total_cents"], cfg)), e(money(i["balance_cents"], cfg) if i["status"] == "issued" else "\u2014"),
           badge(i["display_status"])) for i in rows
    ) or '<tr><td colspan="7" class="empty">No invoices match.</td></tr>'
    return (
        '<div class="bar"><h1>Invoices</h1><a class="btn primary" href="/invoices/new">New invoice</a></div>'
        '<form class="filters" method="get" action="/invoices">'
        '<label>Search<input name="q" value="%s" placeholder="Number or customer"></label>'
        '<label>Status<select name="status">%s</select></label>'
        '<label>Customer<select name="customer_id">%s</select></label><button>Filter</button></form>'
        '<div class="card scroll"><table><thead><tr><th>Number</th><th>Customer</th><th>Issued</th><th>Due</th>'
        '<th class="r">Total</th><th class="r">Balance</th><th>Status</th></tr></thead><tbody>%s</tbody></table></div>'
    ) % (e(filters["q"]), status_opts, cust_opts, body)


def _item_row(item):
    return (
        '<tr class="item"><td style="width:150px"><select class="pick" aria-label="Pick product"><option value="">Custom</option></select></td>'
        '<td><input name="item_desc" value="%s" placeholder="Description"></td>'
        '<td style="width:80px"><input class="num" name="item_qty" value="%s" inputmode="decimal"></td>'
        '<td style="width:110px"><input class="num" name="item_price" value="%s" inputmode="decimal"></td>'
        '<td style="width:75px"><input class="num" name="item_tax" value="%s" inputmode="decimal"></td>'
        '<td class="amt" style="width:100px">0.00</td>'
        '<td style="width:34px"><button type="button" class="rm" title="Remove line">\u00d7</button></td></tr>'
    ) % (e(item["description"]), e(item["quantity"]), e(item["unit_price"]), e(item["tax_rate"]))


def invoice_form(draft, customers, products, cfg, inv=None, error=None):
    err = '<div class="flash err">%s</div>' % e(error) if error else ""
    editing = inv is not None
    is_draft = (not editing) or inv["status"] == "draft"
    cust_opts = '<option value="">\u2014 choose \u2014</option>' + "".join(
        '<option value="%d"%s>%s</option>' % (c["id"], " selected" if str(c["id"]) == draft["customer_id"] else "", e(c["name"]))
        for c in customers)
    disc_opts = "".join(
        '<option value="%s"%s>%s</option>' % (v, " selected" if v == draft["discount_type"] else "", t)
        for v, t in (("none", "No discount"), ("percent", "Percent (%)"), ("fixed", "Fixed amount")))
    blank_item = {"description": "", "quantity": "1", "unit_price": "0.00", "tax_rate": cfg["default_tax_rate"]}
    rows = "".join(_item_row(i) for i in (draft["items"] or [blank_item]))
    blank = _item_row(blank_item)
    pdata = json.dumps([{"name": p["name"], "price": cents_to_str(p["unit_price_cents"]), "tax": p["tax_rate"]}
                        for p in products]).replace("<", "\\u003c")
    buttons = '<button class="primary" name="action" value="issue">Save &amp; issue</button><button name="action" value="draft">Save as draft</button>' \
        if is_draft else '<button class="primary" name="action" value="save">Save changes</button>'
    return (
        '<div class="bar"><h1>%s</h1></div>%s'
        '<form id="invoice-form" method="post" action="/invoices/save"><input type="hidden" name="id" value="%s">'
        '<div class="card"><div class="form-grid"><label>Customer<select name="customer_id" required>%s</select></label>%s%s</div></div>'
        '<div class="card scroll"><table id="items"><thead><tr><th>Product</th><th>Description</th><th class="r">Qty</th>'
        '<th class="r">Price</th><th class="r">%s %%</th><th class="r">Amount</th><th></th></tr></thead><tbody>%s</tbody></table>'
        '<p><button type="button" id="add-row">+ Add line</button></p>'
        '<template id="row-tpl">%s</template>'
        '<script type="application/json" id="products-data">%s</script></div>'
        '<div class="cols"><div class="card"><div class="form-grid"><label>Discount<select name="discount_type">%s</select></label>%s'
        '%s</div></div>'
        '<div class="card totals"><div><span>Subtotal</span><span id="t-sub"></span></div>'
        '<div><span>Discount</span><span id="t-disc"></span></div><div><span>%s</span><span id="t-tax"></span></div>'
        '<div class="grand"><span>Total</span><span id="t-total"></span></div>'
        '<p class="muted" style="margin:8px 0 0;font-size:12px">Preview only \u2013 final amounts are calculated when you save.</p></div></div>'
        '<p class="actions">%s<a class="btn" href="%s">Cancel</a></p></form>'
    ) % ("Edit invoice %s" % e(inv["number"]) if editing else "New invoice", err, inv["id"] if editing else "",
         cust_opts, field("Issue date", "issue_date", draft["issue_date"], "date", True),
         field("Due date", "due_date", draft["due_date"], "date", True), e(cfg["tax_label"]), rows, blank, pdata,
         disc_opts, field("Discount value", "discount_value", draft["discount_value"], extra='inputmode="decimal"'),
         textarea("Notes (shown on invoice)", "notes", draft["notes"], 3, cls="wide"), e(cfg["tax_label"]),
         buttons, "/invoices/%d" % inv["id"] if editing else "/invoices")


def invoice_detail(inv, cfg):
    m = lambda c: e(money(c, cfg))
    c = inv["customer"]
    items = "".join(
        '<tr><td>%s</td><td class="r">%s</td><td class="r">%s</td><td class="r">%s%%</td><td class="r">%s</td></tr>'
        % (e(i["description"]), e(i["quantity"]), m(i["unit_price_cents"]), e(i["tax_rate"]), m(i["line_total_cents"]))
        for i in inv["items"])
    pays = "".join(
        '<tr><td>%s</td><td>%s</td><td>%s</td><td class="r">%s</td><td class="r">%s</td></tr>'
        % (e(p["paid_on"]), e(p["method"]), e(p["reference"]), m(p["amount_cents"]),
           confirm_form("/invoices/%d/payments/%d/delete" % (inv["id"], p["id"]), "Delete", "Delete this payment?"))
        for p in inv["payments"]) or '<tr><td colspan="5" class="empty">No payments recorded.</td></tr>'

    actions = ['<a class="btn primary" href="/invoices/%d/pdf" target="_blank">Download PDF</a>' % inv["id"]]
    if inv["status"] != "void":
        actions.append('<a class="btn" href="/invoices/%d/edit">Edit</a>' % inv["id"])
    if inv["status"] == "draft":
        actions.append('<form class="inline" method="post" action="/invoices/%d/issue"><button>Issue invoice</button></form>' % inv["id"])
        actions.append(confirm_form("/invoices/%d/delete" % inv["id"], "Delete draft", "Delete this draft?", "btn danger"))
    if inv["status"] == "issued" and not inv["payments"]:
        actions.append(confirm_form("/invoices/%d/void" % inv["id"], "Void", "Void this invoice?", "btn danger"))

    totals = '<div><span>Subtotal</span><span>%s</span></div>' % m(inv["subtotal_cents"])
    if inv["discount_cents"]:
        totals += '<div><span>Discount</span><span>-%s</span></div>' % m(inv["discount_cents"])
    totals += '<div><span>%s</span><span>%s</span></div>' % (e(cfg["tax_label"]), m(inv["tax_cents"]))
    totals += '<div class="grand"><span>Total</span><span>%s</span></div>' % m(inv["total_cents"])
    if inv["status"] == "issued":
        totals += '<div><span>Paid</span><span>%s</span></div><div><strong>Balance due</strong><strong>%s</strong></div>' % (
            m(inv["paid_cents"]), m(inv["balance_cents"]))

    payment_form = ""
    if inv["status"] == "issued" and inv["balance_cents"] > 0:
        methods = "".join('<option>%s</option>' % e(x) for x in PAYMENT_METHODS)
        payment_form = (
            '<form class="card" method="post" action="/invoices/%d/payments"><h2>Record payment</h2><div class="form-grid">%s%s'
            '<label>Method<select name="method">%s</select></label>%s</div><p><button class="primary">Add payment</button></p></form>'
        ) % (inv["id"], field("Date", "paid_on", today(), "date", True),
             field("Amount", "amount", cents_to_str(inv["balance_cents"]), required=True, extra='inputmode="decimal"'),
             methods, field("Reference", "reference", ""))
    notes = '<div class="card"><h2>Notes</h2><pre class="addr">%s</pre></div>' % e(inv["notes"]) if inv["notes"] else ""
    return (
        '<div class="bar"><h1>%s %s</h1><div class="actions">%s</div></div>'
        '<div class="card"><div class="cols"><div><h2>Bill to</h2><strong>%s</strong><pre class="addr">%s</pre>'
        '<div class="muted">%s</div></div><dl class="kv"><dt>Issued</dt><dd>%s</dd><dt>Due</dt><dd>%s</dd></dl></div></div>'
        '<div class="card scroll"><table><thead><tr><th>Description</th><th class="r">Qty</th><th class="r">Unit price</th>'
        '<th class="r">%s</th><th class="r">Amount</th></tr></thead><tbody>%s</tbody></table><br><div class="totals">%s</div></div>'
        '%s<div class="card scroll"><h2>Payments</h2><table><thead><tr><th>Date</th><th>Method</th><th>Reference</th>'
        '<th class="r">Amount</th><th></th></tr></thead><tbody>%s</tbody></table></div>%s'
    ) % (e(inv["number"]), badge(inv["display_status"]), " ".join(actions), e(c["name"]), e(c["address"]),
         e(c["email"]), e(inv["issue_date"]), e(inv["due_date"]), e(cfg["tax_label"]), items, totals,
         payment_form, pays, notes)


# --------------------------------------------------------------------------
def reports_page(monthly, outstanding, cfg):
    m = lambda c: e(money(c, cfg))
    mrows = "".join(
        '<tr><td>%s</td><td class="r">%d</td><td class="r">%s</td><td class="r">%s</td><td class="r">%s</td></tr>'
        % (e(r["month"]), r["invoices"], m(r["net_cents"]), m(r["tax_cents"]), m(r["total_cents"])) for r in monthly
    ) or '<tr><td colspan="5" class="empty">No issued invoices yet.</td></tr>'
    orows = "".join(
        '<tr><td><a href="/invoices?customer_id=%d">%s</a></td><td class="r">%s</td></tr>' % (r["id"], e(r["name"]), m(r["balance_cents"]))
        for r in outstanding) or '<tr><td colspan="2" class="empty">No outstanding balances.</td></tr>'
    return (
        '<div class="bar"><h1>Reports</h1><a class="btn" href="/reports/invoices.csv">Export invoices (CSV)</a></div>'
        '<div class="cols"><div class="card scroll"><h2>Sales by month</h2><table><thead><tr><th>Month</th><th class="r">Invoices</th>'
        '<th class="r">Net</th><th class="r">%s</th><th class="r">Total</th></tr></thead><tbody>%s</tbody></table></div>'
        '<div class="card scroll"><h2>Outstanding by customer</h2><table><thead><tr><th>Customer</th><th class="r">Balance</th></tr></thead>'
        "<tbody>%s</tbody></table></div></div>"
    ) % (e(cfg["tax_label"]), mrows, orows)


def settings_page(values, error=None):
    err = '<div class="flash err">%s</div>' % e(error) if error else ""
    f = lambda label, key, **kw: field(label, key, values.get(key), **kw)
    return (
        '<div class="bar"><h1>Settings</h1><a class="btn" href="/backup">Download database backup</a></div>%s'
        '<form class="card" method="post" action="/settings"><h2>Business details (shown on invoices)</h2><div class="form-grid">%s%s%s%s%s</div>'
        '<h2 style="margin-top:22px">Invoicing</h2><div class="form-grid">%s%s%s%s%s%s%s</div>'
        '<p><button class="primary">Save settings</button></p></form>'
    ) % (err, f("Business name", "business_name", required=True), f("Email", "business_email"), f("Phone", "business_phone"),
         f("Tax / registration ID", "business_tax_id"), textarea("Address", "business_address", values.get("business_address"), cls="wide"),
         f("Currency symbol", "currency", required=True), f("Tax label (e.g. Tax, VAT, GST)", "tax_label"),
         f("Invoice prefix", "invoice_prefix"), f("Next invoice number", "next_invoice_number", extra='inputmode="numeric"'),
         f("Default tax rate (%)", "default_tax_rate", extra='inputmode="decimal"'),
         f("Default payment terms (days)", "default_due_days", extra='inputmode="numeric"'),
         textarea("Invoice footer", "invoice_footer", values.get("invoice_footer"), cls="wide"))
