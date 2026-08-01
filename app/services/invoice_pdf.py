"""Pure PDF-generation service: reads Bill/BillItem/Customer state from the DB
and writes a tax-invoice PDF. No Telegram or agent imports here — this module
only knows about the database and the filesystem, so it can be exercised and
debugged directly (see the standalone test at the bottom of this file / the
scripts/ test) without going through the chat loop at all.
"""

import os
import tempfile
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db.models import Bill, BillItem
from app.db.session import async_session_maker

# TODO (Phase 6): no Preference table exists yet for shop identity (name, GSTIN,
# address, etc). Once one does, load real values from it here instead of these
# placeholders. Keeping the placeholders obvious/labeled rather than inventing
# plausible-looking fake shop details.
SHOP_NAME = "[SHOP NAME — placeholder, see Preference table Phase 6]"
SHOP_GSTIN = "[SHOP GSTIN — placeholder, see Preference table Phase 6]"
SHOP_ADDRESS = "[SHOP ADDRESS — placeholder, see Preference table Phase 6]"


async def _load_bill(bill_id: int) -> tuple[Bill, list[BillItem]]:
    async with async_session_maker() as session:
        bill = await session.get(Bill, bill_id)
        if bill is None:
            raise ValueError(f"No bill found with id={bill_id}.")
        if bill.status != "finalized":
            raise ValueError(
                f"Bill {bill_id} is not finalized (status={bill.status}) — "
                "an invoice can only be generated for a finalized bill."
            )
        stmt = (
            select(BillItem)
            .where(BillItem.bill_id == bill_id)
            .options(selectinload(BillItem.product))
            .order_by(BillItem.id)
        )
        items = (await session.execute(stmt)).scalars().all()
        return bill, list(items)


async def generate_invoice_pdf(bill_id: int) -> str:
    """Write a tax-invoice PDF for a finalized bill to a temp path and return
    that path. Raises ValueError for a missing or non-finalized bill."""
    bill, items = await _load_bill(bill_id)

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("InvoiceTitle", parent=styles["Title"], fontSize=18)
    small_style = ParagraphStyle("Small", parent=styles["Normal"], fontSize=9)

    fd, path = tempfile.mkstemp(prefix=f"invoice_bill_{bill_id}_", suffix=".pdf")
    os.close(fd)

    doc = SimpleDocTemplate(path, pagesize=A4, topMargin=20 * mm, bottomMargin=20 * mm)
    elements = []

    elements.append(Paragraph(SHOP_NAME, title_style))
    elements.append(Paragraph(f"GSTIN: {SHOP_GSTIN}", small_style))
    elements.append(Paragraph(SHOP_ADDRESS, small_style))
    elements.append(Spacer(1, 10 * mm))

    elements.append(Paragraph("TAX INVOICE", styles["Heading2"]))
    bill_date = bill.finalized_at or bill.created_at
    elements.append(Paragraph(f"Invoice / Bill No: {bill.id}", styles["Normal"]))
    elements.append(Paragraph(f"Date: {bill_date.strftime('%d-%b-%Y %H:%M')}", styles["Normal"]))
    elements.append(Paragraph(f"Billed to: {bill.customer_name or 'Walk-in Customer'}", styles["Normal"]))
    elements.append(Spacer(1, 6 * mm))

    table_data = [["Product", "Qty", "Unit", "Unit Price", "GST %", "Line Total"]]
    for item in items:
        product_name = item.product.name if item.product else f"product #{item.product_id}"
        unit = item.product.unit if item.product else ""
        table_data.append(
            [
                product_name,
                f"{float(item.quantity):g}",
                unit,
                f"{float(item.unit_price):.2f}",
                f"{float(item.gst_slab):g}%",
                f"{float(item.line_total):.2f}",
            ]
        )

    items_table = Table(table_data, colWidths=[55 * mm, 18 * mm, 15 * mm, 25 * mm, 18 * mm, 28 * mm])
    items_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2f3e46")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("ALIGN", (0, 0), (0, -1), "LEFT"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f2f2")]),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    elements.append(items_table)
    elements.append(Spacer(1, 6 * mm))

    totals_data = [
        ["Subtotal", f"{float(bill.subtotal):.2f}"],
        ["CGST", f"{float(bill.cgst_total):.2f}"],
        ["SGST", f"{float(bill.sgst_total):.2f}"],
        ["Grand Total", f"{float(bill.grand_total):.2f}"],
    ]
    totals_table = Table(totals_data, colWidths=[40 * mm, 28 * mm], hAlign="RIGHT")
    totals_table.setStyle(
        TableStyle(
            [
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                ("LINEABOVE", (0, -1), (-1, -1), 1, colors.black),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
            ]
        )
    )
    elements.append(totals_table)
    elements.append(Spacer(1, 10 * mm))
    elements.append(
        Paragraph(f"Generated {datetime.now().strftime('%d-%b-%Y %H:%M')}", small_style)
    )

    doc.build(elements)
    return path
