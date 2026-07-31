from datetime import datetime, timezone

from langchain_core.tools import tool
from sqlalchemy import select

from app.db.models import Bill, BillItem, Product, StockTxn
from app.db.session import async_session_maker
from app.services.gst import round_line, split_gst
from app.tools.inventory_tools import _ambiguous_message, _find_products


async def _get_draft_bill(session, bill_id: int) -> tuple[Bill | None, str | None]:
    bill = await session.get(Bill, bill_id)
    if bill is None:
        return None, f"No bill found with id={bill_id}."
    if bill.status != "draft":
        return None, f"Bill {bill_id} is {bill.status}, cannot modify."
    return bill, None


async def _resolve_product(session, product_name: str) -> tuple[Product | None, str | None]:
    matches = await _find_products(session, product_name)
    if not matches:
        return None, f"No product found matching '{product_name}'."
    if len(matches) > 1:
        return None, _ambiguous_message(matches)
    return matches[0], None


async def _get_bill_item(session, bill_id: int, product_id: int) -> BillItem | None:
    stmt = select(BillItem).where(BillItem.bill_id == bill_id, BillItem.product_id == product_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


def _compute_line(quantity: float, unit_price: float, slab: float) -> tuple[float, float, float, float]:
    raw_subtotal = quantity * unit_price
    cgst, sgst = split_gst(raw_subtotal, slab)
    line_subtotal = round_line(raw_subtotal)
    line_cgst = round_line(cgst)
    line_sgst = round_line(sgst)
    line_total = line_subtotal + line_cgst + line_sgst
    return line_subtotal, line_cgst, line_sgst, line_total


async def _recalculate_bill_totals(session, bill: Bill) -> None:
    stmt = select(BillItem).where(BillItem.bill_id == bill.id)
    items = (await session.execute(stmt)).scalars().all()
    bill.subtotal = sum(float(i.line_subtotal) for i in items)
    bill.cgst_total = sum(float(i.line_cgst) for i in items)
    bill.sgst_total = sum(float(i.line_sgst) for i in items)
    bill.grand_total = sum(float(i.line_total) for i in items)


@tool
async def start_bill(customer_name: str | None = None) -> str:
    """Start a new draft bill, optionally for a named customer. Returns the bill_id
    to use in subsequent add_item/edit_item/finalize_bill calls — remember it for
    the rest of this billing conversation."""
    async with async_session_maker() as session:
        bill = Bill(customer_name=customer_name, status="draft")
        session.add(bill)
        await session.commit()
        await session.refresh(bill)
        who = customer_name or "a walk-in customer"
        return f"Started bill id={bill.id} for {who}."


@tool
async def add_item(bill_id: int, product_name: str, quantity: float) -> str:
    """Add `quantity` units of a product to a draft bill. If the product already has
    a line on this bill, the quantity is ADDED to the existing line (cumulative) —
    use edit_item instead to set a line's quantity to an absolute value. Rejects the
    request (no changes made) if the resulting line quantity would exceed what's
    currently in stock — this oversell guard is enforced here, not negotiable."""
    if quantity <= 0:
        return "quantity must be positive."
    async with async_session_maker() as session:
        bill, err = await _get_draft_bill(session, bill_id)
        if err:
            return err
        product, err = await _resolve_product(session, product_name)
        if err:
            return err

        existing = await _get_bill_item(session, bill_id, product.id)
        existing_qty = float(existing.quantity) if existing else 0.0
        new_total_qty = existing_qty + quantity

        available = float(product.quantity_on_hand)
        if new_total_qty > available:
            return (
                f"Cannot add {quantity} {product.unit} of '{product.name}' — only "
                f"{available} {product.unit} available in stock."
            )

        line_subtotal, line_cgst, line_sgst, line_total = _compute_line(
            new_total_qty, float(product.sale_price), float(product.gst_slab)
        )
        if existing:
            existing.quantity = new_total_qty
            existing.unit_price = product.sale_price
            existing.gst_slab = product.gst_slab
            existing.line_subtotal = line_subtotal
            existing.line_cgst = line_cgst
            existing.line_sgst = line_sgst
            existing.line_total = line_total
        else:
            session.add(
                BillItem(
                    bill_id=bill.id,
                    product_id=product.id,
                    quantity=new_total_qty,
                    unit_price=product.sale_price,
                    gst_slab=product.gst_slab,
                    line_subtotal=line_subtotal,
                    line_cgst=line_cgst,
                    line_sgst=line_sgst,
                    line_total=line_total,
                )
            )

        await session.flush()
        await _recalculate_bill_totals(session, bill)
        await session.commit()
        return (
            f"Added {quantity} {product.unit} of '{product.name}' to bill {bill.id} "
            f"(line now {new_total_qty} {product.unit}). Bill subtotal={bill.subtotal}, "
            f"CGST={bill.cgst_total}, SGST={bill.sgst_total}, grand_total={bill.grand_total}."
        )


@tool
async def edit_item(bill_id: int, product_name: str, new_quantity: float) -> str:
    """Set an existing bill line's quantity to an absolute value (not cumulative) —
    use this for 'change the quantity of X to N' or 'remove X and add N instead'.
    Set new_quantity to 0 to remove the line entirely. Rejects (no changes made) if
    new_quantity exceeds what's currently in stock."""
    if new_quantity < 0:
        return "new_quantity cannot be negative."
    async with async_session_maker() as session:
        bill, err = await _get_draft_bill(session, bill_id)
        if err:
            return err
        product, err = await _resolve_product(session, product_name)
        if err:
            return err

        existing = await _get_bill_item(session, bill_id, product.id)
        if existing is None:
            return f"'{product.name}' is not on bill {bill_id}. Use add_item to add it."

        available = float(product.quantity_on_hand)
        if new_quantity > available:
            return (
                f"Cannot set '{product.name}' to {new_quantity} {product.unit} — only "
                f"{available} {product.unit} available in stock."
            )

        if new_quantity == 0:
            await session.delete(existing)
            await session.flush()
            await _recalculate_bill_totals(session, bill)
            await session.commit()
            return (
                f"Removed '{product.name}' from bill {bill.id}. Bill subtotal={bill.subtotal}, "
                f"CGST={bill.cgst_total}, SGST={bill.sgst_total}, grand_total={bill.grand_total}."
            )

        line_subtotal, line_cgst, line_sgst, line_total = _compute_line(
            new_quantity, float(product.sale_price), float(product.gst_slab)
        )
        existing.quantity = new_quantity
        existing.unit_price = product.sale_price
        existing.gst_slab = product.gst_slab
        existing.line_subtotal = line_subtotal
        existing.line_cgst = line_cgst
        existing.line_sgst = line_sgst
        existing.line_total = line_total

        await session.flush()
        await _recalculate_bill_totals(session, bill)
        await session.commit()
        return (
            f"Set '{product.name}' to {new_quantity} {product.unit} on bill {bill.id}. "
            f"Bill subtotal={bill.subtotal}, CGST={bill.cgst_total}, SGST={bill.sgst_total}, "
            f"grand_total={bill.grand_total}."
        )


@tool
async def finalize_bill(bill_id: int) -> str:
    """Finalize a draft bill: locks it (status=finalized), decrements stock for
    every line item via a StockTxn (reason='sold'), and returns the final receipt
    totals. Rejects bills that are already finalized or have no items."""
    async with async_session_maker() as session:
        bill = await session.get(Bill, bill_id)
        if bill is None:
            return f"No bill found with id={bill_id}."
        if bill.status == "finalized":
            return f"Bill {bill_id} is already finalized."

        stmt = select(BillItem).where(BillItem.bill_id == bill_id)
        items = (await session.execute(stmt)).scalars().all()
        if not items:
            return f"Bill {bill_id} has no items — add items before finalizing."

        products = {item.product_id: await session.get(Product, item.product_id) for item in items}
        for item in items:
            product = products[item.product_id]
            if float(item.quantity) > float(product.quantity_on_hand):
                return (
                    f"Cannot finalize — '{product.name}' only has "
                    f"{product.quantity_on_hand} {product.unit} in stock but the bill "
                    f"needs {item.quantity}. Use edit_item to adjust the line first."
                )

        for item in items:
            product = products[item.product_id]
            qty = float(item.quantity)
            product.quantity_on_hand = float(product.quantity_on_hand) - qty
            session.add(StockTxn(product_id=product.id, change_qty=-qty, reason="sold"))

        await _recalculate_bill_totals(session, bill)
        bill.status = "finalized"
        bill.finalized_at = datetime.now(timezone.utc)

        await session.commit()
        await session.refresh(bill)
        return (
            f"Bill {bill.id} finalized for {bill.customer_name or 'a walk-in customer'}. "
            f"Subtotal={bill.subtotal}, CGST={bill.cgst_total}, SGST={bill.sgst_total}, "
            f"Grand Total={bill.grand_total}."
        )


ALL_TOOLS = [start_bill, add_item, edit_item, finalize_bill]
