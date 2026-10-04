# Billing App

A simple, self-contained billing and invoicing application written in Python.
It runs on your own computer and opens in your web browser. Data is stored in a
single SQLite file (`data/billing.db`), so there is no server or account to set up.

## Features

- **Customers** and **products / services** catalogue (with default price and tax rate)
- **Invoices**: multiple line items, fractional quantities, per-line tax rates,
  percentage or fixed discount, notes, due dates
- Draft -> issued workflow with **gapless invoice numbers** (numbers are only
  assigned when an invoice is issued); void issued invoices, delete drafts
- **Payments**: partial / full, method and reference; statuses
  (draft, unpaid, partial, overdue, paid, void) are calculated automatically
- **PDF invoices** (A4) with tax breakdown, payments and notes
- **Dashboard** (invoiced, collected, outstanding, overdue), **reports**
  (sales by month, outstanding by customer) and **CSV export**
- **Settings**: business details, currency symbol, tax label (Tax / VAT / GST),
  invoice prefix and next number, default tax rate and payment terms, footer text
- One-click **database backup** download
- Money is handled with `Decimal` and stored as integer cents (no float errors)

## Quick start

Requires Python 3.9 or newer.

```bash
pip install -r requirements.txt     # only dependency: reportlab (for PDFs)
python main.py --demo               # --demo loads sample data into an empty database
```

Your browser opens at http://127.0.0.1:8000. Omit `--demo` to start with an empty
database. On Windows you can double-click `start_billing.bat`; on macOS/Linux run
`./start_billing.sh`.

First steps: open **Settings** and enter your business name, currency and tax label;
add customers (and optionally products); then create an invoice.

### Options

| Option | Meaning |
| --- | --- |
| `--port 9000` | use a different port |
| `--db path/to/file.db` | use another database file |
| `--host 0.0.0.0` | allow other devices on your network (use together with `--password`) |
| `--password secret` | require a login (username `admin`); also read from `$BILLING_PASSWORD` |
| `--no-browser` | don't open the browser automatically |
| `--demo` | load sample data into an empty database |

## Tests

```bash
python -m unittest discover -s tests -v
```

## Project layout

```
main.py                 launcher (command-line options)
billing/
  money.py              Decimal money + tax/discount calculations
  db.py                 SQLite schema and connection handling
  services.py           business rules (customers, invoices, payments, reports)
  pdf.py                PDF invoice generation (ReportLab)
  views.py              HTML pages
  web.py                routes and HTTP server (standard library)
  static/               style.css, app.js
tests/test_billing.py   unit and end-to-end tests
```

## How tax and discounts are calculated

Line amount = quantity x unit price. The invoice-level discount is taken off the
subtotal first; tax is then calculated per tax rate on the discounted amounts and
rounded to the cent (half-up). Total = subtotal - discount + tax.

## Notes and limitations

- Built for a single business / single user. By default it only accepts connections
  from your own computer. If you expose it on a network, always set `--password`
  and put it behind HTTPS (e.g. a reverse proxy), since the built-in server is plain HTTP.
- Back up regularly with Settings -> *Download database backup* (or copy `data/billing.db`).
- Invoices are issued in one currency (the symbol is a display setting).
- PDFs use a system Unicode font (DejaVu or Arial) when available so names and symbols
  such as the rupee sign render correctly; otherwise they fall back to Helvetica.
