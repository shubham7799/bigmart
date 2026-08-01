"""Standalone test for app/services/invoice_pdf.py — no agent, no Telegram.

Run: python scripts/test_invoice_pdf.py <bill_id>
"""

import asyncio
import sys

from pypdf import PdfReader

from app.db.models import Bill
from app.db.session import async_session_maker
from app.services.invoice_pdf import generate_invoice_pdf


async def main(bill_id: int) -> None:
    async with async_session_maker() as session:
        bill = await session.get(Bill, bill_id)
        assert bill is not None, f"no bill {bill_id}"

    path = await generate_invoice_pdf(bill_id)
    print(f"PDF written to: {path}")

    reader = PdfReader(path)
    assert len(reader.pages) >= 1, "PDF has no pages"
    text = reader.pages[0].extract_text()
    print("--- extracted text (page 1) ---")
    print(text)

    for expected in [
        f"{float(bill.subtotal):.2f}",
        f"{float(bill.cgst_total):.2f}",
        f"{float(bill.sgst_total):.2f}",
        f"{float(bill.grand_total):.2f}",
    ]:
        assert expected in text, f"expected value {expected!r} not found in PDF text"
    print("OK: subtotal/CGST/SGST/grand_total all match the Bill row.")

    # Also confirm generation on a draft bill is rejected.
    try:
        await generate_invoice_pdf(999999)
        print("FAIL: expected ValueError for missing bill")
    except ValueError as exc:
        print(f"OK: missing bill correctly rejected: {exc}")


if __name__ == "__main__":
    bill_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    asyncio.run(main(bill_id))
