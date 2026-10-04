"""PDF invoice rendering with ReportLab.

If a Unicode TrueType font (DejaVu / Arial) is found on the system it is used so
names and currency symbols render correctly; otherwise the built-in Helvetica is
used and unsupported characters are replaced.
"""
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .money import compute_totals, fmt_money, fmt_num

_FONT_CANDIDATES = [
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ("/Library/Fonts/Arial.ttf", "/Library/Fonts/Arial Bold.ttf"),
]
_font_cache = None


def _fonts():
    """Return (regular, bold, supported_codepoints or None)."""
    global _font_cache
    if _font_cache is None:
        _font_cache = ("Helvetica", "Helvetica-Bold", None)
        for regular, bold in _FONT_CANDIDATES:
            if Path(regular).exists() and Path(bold).exists():
                try:
                    font = TTFont("BillSans", regular)
                    pdfmetrics.registerFont(font)
                    pdfmetrics.registerFont(TTFont("BillSans-Bold", bold))
                    _font_cache = ("BillSans", "BillSans-Bold", set(font.face.charToGlyph))
                    break
                except Exception:
                    continue
    return _font_cache


def _safe(text):
    """Replace characters the active font cannot draw (they would show as boxes)."""
    text = "" if text is None else str(text)
    supported = _fonts()[2]
    if supported is None:
        return text.encode("cp1252", "replace").decode("cp1252")
    return "".join(ch if (ch in "\n\r\t" or ord(ch) in supported) else "?" for ch in text)


def _currency(symbol):
    safe = _safe(symbol)
    if safe != symbol:
        return "Rs." if symbol == "\u20b9" else safe
    return symbol


def _para(text, style):
    return Paragraph(escape(_safe(text)).replace("\n", "<br/>"), style)


def build_invoice_pdf(inv, cfg):
    """inv: dict from services.get_invoice(); cfg: settings dict. Returns PDF bytes."""
    regular, bold, _ = _fonts()
    sym = _currency(cfg["currency"])
    money = lambda cents: fmt_money(cents, sym)
    accent = colors.HexColor("#2f4f8f")
    muted = colors.HexColor("#6b7280")

    base = ParagraphStyle("base", fontName=regular, fontSize=9.5, leading=13)
    small = ParagraphStyle("small", parent=base, fontSize=8.5, textColor=muted, leading=11.5)
    label = ParagraphStyle("label", parent=small, fontName=bold, textColor=accent)
    right = ParagraphStyle("right", parent=base, alignment=TA_RIGHT)
    right_bold = ParagraphStyle("rightb", parent=right, fontName=bold)
    title = ParagraphStyle("title", parent=base, fontName=bold, fontSize=20, leading=24, textColor=accent)
    biz = ParagraphStyle("biz", parent=base, fontName=bold, fontSize=15, leading=19)
    head = ParagraphStyle("head", parent=base, fontName=bold, textColor=colors.white)
    head_r = ParagraphStyle("headr", parent=head, alignment=TA_RIGHT)

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
        bottomMargin=16 * mm, title="Invoice " + _safe(inv["number"]), author=_safe(cfg["business_name"]),
    )
    width = A4[0] - 36 * mm
    story = []

    # --- header -----------------------------------------------------------
    status = inv["display_status"].upper()
    header = Table(
        [[_para(cfg["business_name"], biz),
          [Paragraph("INVOICE", ParagraphStyle("t", parent=title, alignment=TA_RIGHT)),
           Paragraph(escape(_safe(inv["number"])), right_bold),
           Paragraph(status, ParagraphStyle("st", parent=right, textColor=muted, fontSize=8.5))]]],
        colWidths=[width * 0.58, width * 0.42],
    )
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [header, Spacer(1, 4 * mm)]

    # --- parties & dates --------------------------------------------------
    seller = [cfg["business_address"], cfg["business_phone"], cfg["business_email"]]
    if cfg["business_tax_id"]:
        seller.append("%s: %s" % (cfg["tax_label"] + " ID", cfg["business_tax_id"]))
    cust = inv["customer"]
    buyer = [cust["name"], cust["address"], cust["email"], cust["phone"]]
    if cust["tax_id"]:
        buyer.append("%s: %s" % (cfg["tax_label"] + " ID", cust["tax_id"]))
    dates = Table(
        [[Paragraph("Issue date", small), Paragraph(inv["issue_date"], right)],
         [Paragraph("Due date", small), Paragraph(inv["due_date"], right)]],
        colWidths=[22 * mm, 30 * mm],
    )
    dates.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    parties = Table(
        [[Paragraph("FROM", label), Paragraph("BILL TO", label), ""],
         [_para("\n".join(x for x in seller if x), small),
          _para("\n".join(x for x in buyer if x), base), dates]],
        colWidths=[width * 0.36, width * 0.36, width * 0.28],
    )
    parties.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                 ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor("#d1d5db")),
                                 ("BOTTOMPADDING", (0, 0), (-1, 0), 3)]))
    story += [parties, Spacer(1, 7 * mm)]

    # --- line items -------------------------------------------------------
    rows = [[Paragraph("#", head), Paragraph("Description", head), Paragraph("Qty", head_r),
             Paragraph("Unit price", head_r), Paragraph("%s %%" % _safe(cfg["tax_label"]), head_r),
             Paragraph("Amount", head_r)]]
    for n, it in enumerate(inv["items"], start=1):
        rows.append([
            Paragraph(str(n), base), _para(it["description"], base), Paragraph(it["quantity"], right),
            Paragraph(escape(money(it["unit_price_cents"])), right),
            Paragraph(fmt_num(it["tax_rate"]), right),
            Paragraph(escape(money(it["line_total_cents"])), right),
        ])
    items = Table(rows, colWidths=[8 * mm, width - 94 * mm, 16 * mm, 24 * mm, 18 * mm, 28 * mm],
                  repeatRows=1)
    items.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), accent),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, colors.HexColor("#e5e7eb")),
    ]))
    story += [items, Spacer(1, 4 * mm)]

    # --- totals -----------------------------------------------------------
    breakdown = compute_totals(
        [{"quantity": Decimal(i["quantity"]), "unit_price_cents": i["unit_price_cents"],
          "tax_rate": Decimal(i["tax_rate"])} for i in inv["items"]],
        inv["discount_type"], Decimal(inv["discount_value"]),
    )["tax_breakdown"]
    lines = [("Subtotal", money(inv["subtotal_cents"]), False)]
    if inv["discount_cents"]:
        lines.append(("Discount", "-" + money(inv["discount_cents"]), False))
    for b in breakdown:
        if b["rate"] > 0:
            lines.append(("%s (%s%%)" % (cfg["tax_label"], fmt_num(b["rate"])), money(b["tax_cents"]), False))
    lines.append(("Total", money(inv["total_cents"]), True))
    if inv["paid_cents"]:
        lines.append(("Paid", "-" + money(inv["paid_cents"]), False))
        lines.append(("Balance due", money(inv["balance_cents"]), True))
    totals = Table(
        [[Paragraph(escape(_safe(k)), right_bold if b else right), Paragraph(escape(v), right_bold if b else right)]
         for k, v, b in lines],
        colWidths=[34 * mm, 32 * mm], hAlign="RIGHT",
    )
    style = [("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]
    for idx, (_, _, is_bold) in enumerate(lines):
        if is_bold:
            style.append(("LINEABOVE", (0, idx), (-1, idx), 0.8, accent))
    totals.setStyle(TableStyle(style))
    story.append(totals)

    # --- payments, notes, footer -----------------------------------------
    if inv["payments"]:
        story += [Spacer(1, 6 * mm), Paragraph("PAYMENTS RECEIVED", label)]
        for p in inv["payments"]:
            text = "%s \u2014 %s" % (p["paid_on"], money(p["amount_cents"]))
            extra = ", ".join(x for x in (p["method"], p["reference"]) if x)
            story.append(_para(text + (" (%s)" % extra if extra else ""), small))
    if inv["notes"]:
        story += [Spacer(1, 6 * mm), Paragraph("NOTES", label), _para(inv["notes"], base)]
    if cfg["invoice_footer"]:
        story += [Spacer(1, 10 * mm), _para(cfg["invoice_footer"], small)]

    doc.build(story)
    return buf.getvalue()
