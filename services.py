"""Business logic: customers, products, invoices, payments, reports, settings.

Functions take an open sqlite3 connection. Validation problems raise
ValueError (shown to the user); missing records raise LookupError (HTTP 404).
"""
import csv
import io
import uuid
from datetime import date, timedelta
from decimal import Decimal

from .money import compute_totals, cents_to_str, fmt_num, to_cents, to_dec

DEFAULT_SETTINGS = {
    "business_name": "My Business",
    "business_address": "",
    "business_phone": "",
    "business_email": "",
    "business_tax_id": "",
    "currency": "$",
    "tax_label": "Tax",
    "invoice_prefix": "INV-",
    "next_invoice_number": "1",
    "default_tax_rate": "0",
    "default_due_days": "14",
    "invoice_footer": "Thank you for your business.",
}

PAYMENT_METHODS = ["Cash", "Bank transfer", "Card", "UPI", "Cheque", "Other"]


class Form:
    """Thin wrapper over parse_qs output: first value via get(), all via getlist()."""

    def __init__(self, data=None):
        self._d = {}
        for key, val in (data or {}).items():
            self._d[key] = [val] if isinstance(val, str) else list(val)

    def get(self, key, default=""):
        vals = self._d.get(key)
        return vals[0] if vals else default

    def getlist(self, key):
        return list(self._d.get(key, []))


def today():
    return date.today().isoformat()


def clean(value):
    return (value or "").strip()


def parse_date(value, label):
    try:
        return date.fromisoformat(clean(value)).isoformat()
    except ValueError:
        raise ValueError("%s must be a valid date (YYYY-MM-DD)." % label)


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
def get_settings(conn):
    cfg = dict(DEFAULT_SETTINGS)
    for row in conn.execute("SELECT key, value FROM settings"):
        if row["key"] in cfg:
            cfg[row["key"]] = row["value"]
    return cfg


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def save_settings(conn, form):
    values = {key: clean(form.get(key, DEFAULT_SETTINGS[key])) for key in DEFAULT_SETTINGS}
    if not values["business_name"]:
        raise ValueError("Business name is required.")
    if not values["currency"]:
        raise ValueError("Currency symbol is required.")
    try:
        if int(values["next_invoice_number"]) < 1:
            raise ValueError
    except ValueError:
        raise ValueError("Next invoice number must be a whole number of 1 or more.")
    try:
        if not 0 <= int(values["default_due_days"]) <= 365:
            raise ValueError
    except ValueError:
        raise ValueError("Default payment terms must be between 0 and 365 days.")
    rate = to_dec(values["default_tax_rate"])
    if not 0 <= rate <= 100:
        raise ValueError("Default tax rate must be between 0 and 100.")
    values["default_tax_rate"] = fmt_num(rate)
    for key, value in values.items():
        set_setting(conn, key, value)


# --------------------------------------------------------------------------
# Customers
# --------------------------------------------------------------------------
def list_customers(conn, q=""):
    like = "%" + clean(q) + "%"
    return conn.execute(
        """
        SELECT c.*,
          (SELECT COUNT(*) FROM invoices i
            WHERE i.customer_id = c.id AND i.status != 'draft') AS invoice_count,
          COALESCE((SELECT SUM(i.total_cents) FROM invoices i
            WHERE i.customer_id = c.id AND i.status = 'issued'), 0)
          - COALESCE((SELECT SUM(p.amount_cents) FROM payments p
            JOIN invoices i ON i.id = p.invoice_id
            WHERE i.customer_id = c.id AND i.status = 'issued'), 0) AS balance_cents
        FROM customers c
        WHERE c.name LIKE ? OR c.email LIKE ? OR c.phone LIKE ?
        ORDER BY c.name COLLATE NOCASE
        """,
        (like, like, like),
    ).fetchall()


def get_customer(conn, customer_id):
    row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if not row:
        raise LookupError("Customer not found")
    return dict(row)


def save_customer(conn, customer_id, form):
    name = clean(form.get("name"))
    email = clean(form.get("email"))
    if not name:
        raise ValueError("Customer name is required.")
    if email and "@" not in email:
        raise ValueError("Please enter a valid email address.")
    values = (name, email, clean(form.get("phone")), clean(form.get("address")), clean(form.get("tax_id")))
    if customer_id:
        get_customer(conn, customer_id)
        conn.execute(
            "UPDATE customers SET name=?, email=?, phone=?, address=?, tax_id=? WHERE id=?",
            values + (customer_id,),
        )
        return customer_id
    cur = conn.execute(
        "INSERT INTO customers (name, email, phone, address, tax_id) VALUES (?, ?, ?, ?, ?)", values
    )
    return cur.lastrowid


def delete_customer(conn, customer_id):
    get_customer(conn, customer_id)
    if conn.execute("SELECT 1 FROM invoices WHERE customer_id = ?", (customer_id,)).fetchone():
        raise ValueError("This customer has invoices and cannot be deleted.")
    conn.execute("DELETE FROM customers WHERE id = ?", (customer_id,))


# --------------------------------------------------------------------------
# Products / services
# --------------------------------------------------------------------------
def list_products(conn, q=""):
    like = "%" + clean(q) + "%"
    return conn.execute(
        "SELECT * FROM products WHERE name LIKE ? OR sku LIKE ? ORDER BY name COLLATE NOCASE",
        (like, like),
    ).fetchall()


def get_product(conn, product_id):
    row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if not row:
        raise LookupError("Product not found")
    return dict(row)


def save_product(conn, product_id, form):
    name = clean(form.get("name"))
    if not name:
        raise ValueError("Name is required.")
    price = to_cents(form.get("price"))
    if price < 0:
        raise ValueError("Price cannot be negative.")
    rate = to_dec(form.get("tax_rate"))
    if not 0 <= rate <= 100:
        raise ValueError("Tax rate must be between 0 and 100.")
    values = (name, clean(form.get("sku")), price, fmt_num(rate))
    if product_id:
        get_product(conn, product_id)
        conn.execute(
            "UPDATE products SET name=?, sku=?, unit_price_cents=?, tax_rate=? WHERE id=?",
            values + (product_id,),
        )
        return product_id
    cur = conn.execute(
        "INSERT INTO products (name, sku, unit_price_cents, tax_rate) VALUES (?, ?, ?, ?)", values
    )
    return cur.lastrowid


def delete_product(conn, product_id):
    get_product(conn, product_id)
    conn.execute("DELETE FROM products WHERE id = ?", (product_id,))


# --------------------------------------------------------------------------
# Invoices
# --------------------------------------------------------------------------
INVOICE_SELECT = """
    SELECT i.*, c.name AS customer_name, c.email AS customer_email,
      COALESCE((SELECT SUM(p.amount_cents) FROM payments p WHERE p.invoice_id = i.id), 0) AS paid_cents
    FROM invoices i JOIN customers c ON c.id = i.customer_id
"""

STATUSES = ["draft", "unpaid", "partial", "overdue", "paid", "void"]


def display_status(inv, on=None):
    """Draft / void are stored; the rest are derived from payments and due date."""
    on = on or today()
    if inv["status"] in ("draft", "void"):
        return inv["status"]
    if inv["paid_cents"] >= inv["total_cents"]:
        return "paid"
    if inv["due_date"] < on:
        return "overdue"
    return "partial" if inv["paid_cents"] > 0 else "unpaid"


def _decorate(row):
    inv = dict(row)
    inv["balance_cents"] = inv["total_cents"] - inv["paid_cents"]
    inv["display_status"] = display_status(inv)
    return inv


def list_invoices(conn, status="", q="", customer_id=None):
    sql, args = INVOICE_SELECT, []
    where = []
    if customer_id:
        where.append("i.customer_id = ?")
        args.append(int(customer_id))
    if clean(q):
        where.append("(i.number LIKE ? OR c.name LIKE ?)")
        args += ["%" + clean(q) + "%"] * 2
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY i.issue_date DESC, i.id DESC"
    invoices = [_decorate(r) for r in conn.execute(sql, args)]
    if status:
        invoices = [i for i in invoices if i["display_status"] == status]
    return invoices


def get_invoice(conn, invoice_id):
    row = conn.execute(INVOICE_SELECT + " WHERE i.id = ?", (invoice_id,)).fetchone()
    if not row:
        raise LookupError("Invoice not found")
    inv = _decorate(row)
    inv["items"] = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY position", (invoice_id,)
        )
    ]
    inv["payments"] = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM payments WHERE invoice_id = ? ORDER BY paid_on, id", (invoice_id,)
        )
    ]
    inv["customer"] = get_customer(conn, inv["customer_id"])
    return inv


def blank_draft(conn, customer_id=""):
    cfg = get_settings(conn)
    issue = date.today()
    due = issue + timedelta(days=int(cfg["default_due_days"]))
    return {
        "customer_id": str(customer_id or ""),
        "issue_date": issue.isoformat(),
        "due_date": due.isoformat(),
        "discount_type": "none",
        "discount_value": "0",
        "notes": "",
        "items": [{"description": "", "quantity": "1", "unit_price": "0.00", "tax_rate": cfg["default_tax_rate"]}],
    }


def invoice_to_draft(inv):
    return {
        "customer_id": str(inv["customer_id"]),
        "issue_date": inv["issue_date"],
        "due_date": inv["due_date"],
        "discount_type": inv["discount_type"],
        "discount_value": inv["discount_value"],
        "notes": inv["notes"],
        "items": [
            {
                "description": it["description"],
                "quantity": it["quantity"],
                "unit_price": cents_to_str(it["unit_price_cents"]),
                "tax_rate": it["tax_rate"],
            }
            for it in inv["items"]
        ],
    }


def draft_from_form(form):
    rows = zip(
        form.getlist("item_desc"), form.getlist("item_qty"),
        form.getlist("item_price"), form.getlist("item_tax"),
    )
    items = [
        {"description": d, "quantity": q, "unit_price": p, "tax_rate": t}
        for d, q, p, t in rows
        if clean(d) or clean(p)
    ]
    return {
        "customer_id": clean(form.get("customer_id")),
        "issue_date": clean(form.get("issue_date")),
        "due_date": clean(form.get("due_date")),
        "discount_type": clean(form.get("discount_type")) or "none",
        "discount_value": clean(form.get("discount_value")) or "0",
        "notes": clean(form.get("notes")),
        "items": items,
    }


def validate_draft(draft):
    """Turn raw form strings into clean typed values plus computed totals."""
    try:
        customer_id = int(draft["customer_id"])
    except (TypeError, ValueError):
        raise ValueError("Please choose a customer.")
    issue = parse_date(draft["issue_date"], "Issue date")
    due = parse_date(draft["due_date"], "Due date")
    if due < issue:
        raise ValueError("Due date cannot be before the issue date.")
    if not draft["items"]:
        raise ValueError("Add at least one line item.")

    items = []
    for n, raw in enumerate(draft["items"], start=1):
        description = clean(raw["description"])
        if not description:
            raise ValueError("Line %d: description is required." % n)
        qty = to_dec(raw["quantity"], "1")
        if qty <= 0:
            raise ValueError("Line %d: quantity must be greater than zero." % n)
        price = to_cents(raw["unit_price"])
        if price < 0:
            raise ValueError("Line %d: price cannot be negative." % n)
        rate = to_dec(raw["tax_rate"])
        if not 0 <= rate <= 100:
            raise ValueError("Line %d: tax rate must be between 0 and 100." % n)
        items.append({"description": description, "quantity": qty, "unit_price_cents": price, "tax_rate": rate})

    dtype = draft["discount_type"]
    if dtype not in ("none", "percent", "fixed"):
        raise ValueError("Invalid discount type.")
    dvalue = to_dec(draft["discount_value"]) if dtype != "none" else Decimal(0)
    if dvalue < 0:
        raise ValueError("Discount cannot be negative.")
    if dtype == "percent" and dvalue > 100:
        raise ValueError("Percentage discount cannot exceed 100.")

    totals = compute_totals(items, dtype, dvalue)
    if dtype == "fixed" and round(dvalue * 100) > totals["subtotal"]:
        raise ValueError("Discount cannot be larger than the subtotal.")
    return {
        "customer_id": customer_id, "issue_date": issue, "due_date": due, "items": items,
        "discount_type": dtype, "discount_value": fmt_num(dvalue), "notes": draft["notes"],
        "totals": totals,
    }


def allocate_number(conn):
    """Next gapless invoice number; skips numbers that are already taken."""
    cfg = get_settings(conn)
    n = int(cfg["next_invoice_number"] or 1)
    while True:
        number = "%s%04d" % (cfg["invoice_prefix"], n)
        if not conn.execute("SELECT 1 FROM invoices WHERE number = ?", (number,)).fetchone():
            break
        n += 1
    set_setting(conn, "next_invoice_number", n + 1)
    return number


def issue_invoice(conn, invoice_id):
    inv = get_invoice(conn, invoice_id)
    if inv["status"] != "draft":
        raise ValueError("Only draft invoices can be issued.")
    conn.execute(
        "UPDATE invoices SET status = 'issued', number = ? WHERE id = ?",
        (allocate_number(conn), invoice_id),
    )


def save_invoice(conn, invoice_id, draft, action="draft"):
    """Create or update an invoice. action='issue' also issues a draft."""
    data = validate_draft(draft)
    if not conn.execute("SELECT 1 FROM customers WHERE id = ?", (data["customer_id"],)).fetchone():
        raise ValueError("Please choose a customer.")
    totals = data["totals"]
    head = (
        data["customer_id"], data["issue_date"], data["due_date"], data["discount_type"],
        data["discount_value"], data["notes"], totals["subtotal"], totals["discount"],
        totals["tax"], totals["total"],
    )

    status = "draft"
    if invoice_id:
        row = conn.execute("SELECT status FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        if not row:
            raise LookupError("Invoice not found")
        status = row["status"]
        if status == "void":
            raise ValueError("Void invoices cannot be edited.")
        paid = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM payments WHERE invoice_id = ?", (invoice_id,)
        ).fetchone()[0]
        if paid > totals["total"]:
            raise ValueError("The new total is lower than the payments already recorded.")
        conn.execute(
            "UPDATE invoices SET customer_id=?, issue_date=?, due_date=?, discount_type=?, "
            "discount_value=?, notes=?, subtotal_cents=?, discount_cents=?, tax_cents=?, "
            "total_cents=? WHERE id=?",
            head + (invoice_id,),
        )
        conn.execute("DELETE FROM invoice_items WHERE invoice_id = ?", (invoice_id,))
    else:
        cur = conn.execute(
            "INSERT INTO invoices (number, customer_id, issue_date, due_date, discount_type, "
            "discount_value, notes, subtotal_cents, discount_cents, tax_cents, total_cents) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("DRAFT-tmp-" + uuid.uuid4().hex,) + head,
        )
        invoice_id = cur.lastrowid
        conn.execute("UPDATE invoices SET number = ? WHERE id = ?", ("DRAFT-%d" % invoice_id, invoice_id))

    for pos, (item, line) in enumerate(zip(data["items"], totals["line_totals"])):
        conn.execute(
            "INSERT INTO invoice_items (invoice_id, position, description, quantity, "
            "unit_price_cents, tax_rate, line_total_cents) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (invoice_id, pos, item["description"], fmt_num(item["quantity"]),
             item["unit_price_cents"], fmt_num(item["tax_rate"]), line),
        )
    if action == "issue" and status == "draft":
        issue_invoice(conn, invoice_id)
    return invoice_id


def void_invoice(conn, invoice_id):
    inv = get_invoice(conn, invoice_id)
    if inv["status"] != "issued":
        raise ValueError("Only issued invoices can be voided.")
    if inv["payments"]:
        raise ValueError("Delete the recorded payments before voiding this invoice.")
    conn.execute("UPDATE invoices SET status = 'void' WHERE id = ?", (invoice_id,))


def delete_invoice(conn, invoice_id):
    inv = get_invoice(conn, invoice_id)
    if inv["status"] != "draft":
        raise ValueError("Only draft invoices can be deleted. Void issued invoices instead.")
    conn.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))


# --------------------------------------------------------------------------
# Payments
# --------------------------------------------------------------------------
def add_payment(conn, invoice_id, form):
    inv = get_invoice(conn, invoice_id)
    if inv["status"] != "issued":
        raise ValueError("Payments can only be recorded on issued invoices.")
    paid_on = parse_date(form.get("paid_on"), "Payment date")
    amount = to_cents(form.get("amount"))
    if amount <= 0:
        raise ValueError("Payment amount must be greater than zero.")
    if amount > inv["balance_cents"]:
        raise ValueError("Payment is more than the balance due.")
    conn.execute(
        "INSERT INTO payments (invoice_id, paid_on, amount_cents, method, reference) VALUES (?, ?, ?, ?, ?)",
        (invoice_id, paid_on, amount, clean(form.get("method")), clean(form.get("reference"))),
    )


def delete_payment(conn, invoice_id, payment_id):
    cur = conn.execute("DELETE FROM payments WHERE id = ? AND invoice_id = ?", (payment_id, invoice_id))
    if not cur.rowcount:
        raise LookupError("Payment not found")


# --------------------------------------------------------------------------
# Dashboard & reports
# --------------------------------------------------------------------------
def dashboard(conn):
    invoices = list_invoices(conn)
    issued = [i for i in invoices if i["status"] == "issued"]
    overdue = [i for i in issued if i["display_status"] == "overdue"]
    return {
        "invoiced": sum(i["total_cents"] for i in issued),
        "collected": sum(i["paid_cents"] for i in issued),
        "outstanding": sum(i["balance_cents"] for i in issued),
        "overdue": sum(i["balance_cents"] for i in overdue),
        "overdue_count": len(overdue),
        "draft_count": sum(1 for i in invoices if i["status"] == "draft"),
        "customer_count": conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0],
        "recent": invoices[:6],
        "overdue_list": sorted(overdue, key=lambda i: i["due_date"])[:6],
    }


def monthly_sales(conn):
    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT substr(issue_date, 1, 7) AS month, COUNT(*) AS invoices,
                   SUM(subtotal_cents - discount_cents) AS net_cents,
                   SUM(tax_cents) AS tax_cents, SUM(total_cents) AS total_cents
            FROM invoices WHERE status = 'issued'
            GROUP BY month ORDER BY month DESC
            """
        )
    ]


def outstanding_by_customer(conn):
    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT c.id, c.name,
                   SUM(i.total_cents) - COALESCE(SUM(
                     (SELECT SUM(p.amount_cents) FROM payments p WHERE p.invoice_id = i.id)), 0)
                     AS balance_cents
            FROM invoices i JOIN customers c ON c.id = i.customer_id
            WHERE i.status = 'issued'
            GROUP BY c.id HAVING balance_cents > 0 ORDER BY balance_cents DESC
            """
        )
    ]


def invoices_csv(conn):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Number", "Customer", "Issue date", "Due date", "Status",
                     "Subtotal", "Discount", "Tax", "Total", "Paid", "Balance"])
    for i in list_invoices(conn):
        writer.writerow([
            i["number"], i["customer_name"], i["issue_date"], i["due_date"], i["display_status"],
            cents_to_str(i["subtotal_cents"]), cents_to_str(i["discount_cents"]),
            cents_to_str(i["tax_cents"]), cents_to_str(i["total_cents"]),
            cents_to_str(i["paid_cents"]), cents_to_str(i["balance_cents"]),
        ])
    return out.getvalue()


# --------------------------------------------------------------------------
# Demo data
# --------------------------------------------------------------------------
def seed_demo(conn):
    """Load sample data (only into an empty database)."""
    if conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]:
        return
    set_setting(conn, "business_name", "Acme Studio")
    set_setting(conn, "business_address", "12 Market Street\nSpringfield")
    set_setting(conn, "business_email", "hello@acme.example")
    set_setting(conn, "default_tax_rate", "10")
    customers = [
        ("Globex Corporation", "accounts@globex.example", "555-0101", "1 Globex Way\nCapital City"),
        ("Initech", "billing@initech.example", "555-0102", "4120 Freidrich Lane\nAustin"),
        ("Umbrella Retail", "ap@umbrella.example", "555-0103", "88 Raccoon Road\nRaccoon City"),
        ("Wayne Supplies", "orders@wayne.example", "555-0104", "1007 Mountain Drive\nGotham"),
    ]
    ids = [save_customer(conn, None, Form(dict(zip(("name", "email", "phone", "address"), c)))) for c in customers]
    for name, sku, price, tax in [
        ("Consulting (hour)", "SRV-001", "90", "10"), ("Website design", "SRV-002", "1500", "10"),
        ("Monthly maintenance", "SRV-003", "250", "10"), ("Logo pack", "PRD-010", "320", "10"),
        ("Domain renewal", "PRD-011", "18", "0"),
    ]:
        save_product(conn, None, Form({"name": name, "sku": sku, "price": price, "tax_rate": tax}))

    def make(cust, days_ago, items, due_days=14, action="issue", discount=("none", "0")):
        d = blank_draft(conn, ids[cust])
        issue = date.today() - timedelta(days=days_ago)
        d.update(issue_date=issue.isoformat(), due_date=(issue + timedelta(days=due_days)).isoformat(),
                 discount_type=discount[0], discount_value=discount[1])
        d["items"] = [{"description": n, "quantity": q, "unit_price": p, "tax_rate": t} for n, q, p, t in items]
        return save_invoice(conn, None, d, action)

    paid = make(0, 40, [("Website design", "1", "1500", "10"), ("Domain renewal", "1", "18", "0")])
    add_payment(conn, paid, Form({"paid_on": today(), "amount": cents_to_str(get_invoice(conn, paid)["total_cents"]), "method": "Bank transfer"}))
    part = make(1, 20, [("Consulting (hour)", "12", "90", "10")], discount=("percent", "5"))
    add_payment(conn, part, Form({"paid_on": today(), "amount": "500", "method": "Card"}))
    make(2, 30, [("Monthly maintenance", "3", "250", "10")])
    make(3, 3, [("Logo pack", "2", "320", "10")])
    make(0, 0, [("Consulting (hour)", "4", "90", "10")], action="draft")
