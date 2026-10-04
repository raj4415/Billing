"""Money and tax arithmetic.

All monetary amounts are stored as integers in minor units (cents) and all
calculations use Decimal with ROUND_HALF_UP, so there are no float errors.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

ONE = Decimal(1)


def to_dec(value, default="0"):
    """Parse user input into a finite Decimal (commas are ignored)."""
    text = str(value if value is not None else "").strip().replace(",", "")
    if not text:
        text = default
    try:
        d = Decimal(text)
    except InvalidOperation:
        raise ValueError("'%s' is not a valid number." % value)
    if not d.is_finite():
        raise ValueError("'%s' is not a valid number." % value)
    return d


def round_int(d):
    return int(Decimal(d).quantize(ONE, rounding=ROUND_HALF_UP))


def to_cents(value):
    return round_int(to_dec(value) * 100)


def cents_to_str(cents):
    """Plain machine-friendly amount, e.g. 123456 -> '1234.56'."""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return "%s%d.%02d" % (sign, cents // 100, cents % 100)


def fmt_money(cents, symbol="$"):
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return "%s%s%s.%02d" % (sign, symbol, format(cents // 100, ","), cents % 100)


def fmt_num(value):
    """Decimal -> string without exponent or trailing zeros (10.00 -> '10')."""
    return format(Decimal(value).normalize(), "f")


def compute_totals(items, discount_type="none", discount_value=Decimal(0)):
    """Calculate invoice totals.

    items: list of dicts with Decimal 'quantity', int 'unit_price_cents' and
           Decimal 'tax_rate' (percent).
    The invoice-level discount is applied to the subtotal *before* tax; tax is
    then calculated per tax rate on the discounted amounts.
    """
    line_totals = [round_int(i["quantity"] * i["unit_price_cents"]) for i in items]
    subtotal = sum(line_totals)

    if discount_type == "percent":
        discount = round_int(Decimal(subtotal) * discount_value / 100)
    elif discount_type == "fixed":
        discount = round_int(discount_value * 100)
    else:
        discount = 0
    discount = max(0, min(discount, subtotal))

    factor = Decimal(subtotal - discount) / Decimal(subtotal) if subtotal else Decimal(0)
    groups = {}
    for item, line in zip(items, line_totals):
        groups[item["tax_rate"]] = groups.get(item["tax_rate"], Decimal(0)) + line * factor

    breakdown = [
        {"rate": rate, "taxable_cents": round_int(base), "tax_cents": round_int(base * rate / 100)}
        for rate, base in sorted(groups.items())
    ]
    tax = sum(b["tax_cents"] for b in breakdown)
    return {
        "line_totals": line_totals,
        "subtotal": subtotal,
        "discount": discount,
        "tax_breakdown": breakdown,
        "tax": tax,
        "total": subtotal - discount + tax,
    }
