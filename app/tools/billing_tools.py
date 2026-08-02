from datetime import datetime, timezone

from langchain_core.tools import tool
from sqlalchemy import select

from app.db.models import Bill, BillItem, Product, StockTxn
from app.db.session import async_session_maker
from app.services.gst import round_line, split_gst
from app.tools.inventory_tools import _ambiguous_message, _find_products
from app.tools.khata_tools import add_credit
from app.tools.preference_tools import get_preference_value


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
async def finalize_bill(bill_id: int, on_credit: bool = False, payment_method: str | None = None) -> str:
    """Finalize a draft bill: locks it (status=finalized), decrements stock for
    every line item via a StockTxn (reason='sold'), and returns the final receipt
    totals. Rejects bills that are already finalized or have no items. If
    on_credit is true, the bill's grand_total is added to the customer's khata
    (credit ledger) instead of being treated as paid in full — requires the bill
    to have a customer_name set. If payment_method isn't given, falls back to
    the shop's "default_payment_method" preference if one is set."""
    # NOTE: this whole function runs in a single DB transaction (one session, one
    # commit at the end). It relies on SELECT ... FOR UPDATE row locks, which
    # Postgres honors as real per-row locks — a second transaction trying to lock
    # an already-locked row blocks until the first commits/rolls back, then sees
    # the updated row. SQLite does NOT do per-row locking: it locks the whole
    # database file on write, so FOR UPDATE there doesn't buy the same
    # fine-grained concurrency guarantee (two writers just serialize on the whole
    # DB rather than only on the rows they actually touch, and some SQLite
    # drivers ignore FOR UPDATE syntax entirely). This code assumes Postgres —
    # settings.database_url should point at Postgres in any environment where
    # concurrent finalize_bill calls are expected, not at a SQLite file.
    async with async_session_maker() as session:
        bill = (
            await session.execute(select(Bill).where(Bill.id == bill_id).with_for_update())
        ).scalar_one_or_none()
        if bill is None:
            return f"No bill found with id={bill_id}."
        if bill.status == "finalized":
            # Lost the race to another finalize_bill call for this same bill_id —
            # it already committed and released the lock by the time we acquired
            # it, so we just observe the final state and no-op.
            return f"Bill {bill_id} is already finalized."

        stmt = select(BillItem).where(BillItem.bill_id == bill_id)
        items = (await session.execute(stmt)).scalars().all()
        if not items:
            return f"Bill {bill_id} has no items — add items before finalizing."

        if on_credit and not bill.customer_name:
            return (
                f"Cannot finalize bill {bill_id} on credit — it has no customer_name "
                "set, so there's no one to add the khata entry for."
            )

        # Lock every product row this bill touches, in a fixed ascending-id order.
        # A consistent lock order across all concurrent finalize_bill calls (even
        # ones for different bills) prevents lock-ordering deadlocks when two
        # bills share products.
        product_ids = sorted({item.product_id for item in items})
        products = {}
        for product_id in product_ids:
            product = (
                await session.execute(
                    select(Product).where(Product.id == product_id).with_for_update()
                )
            ).scalar_one_or_none()
            products[product_id] = product

        # Re-check stock now that we hold the locks: another bill may have been
        # finalized against the same product(s) between this bill's add_item
        # calls and this finalize call, so the Phase 2 oversell check at add_item
        # time is not sufficient on its own.
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

        # --- preference hook (Phase 6) ---
        # If the caller didn't pin down a payment method for this specific bill,
        # fall back to the shop's standing "default_payment_method" preference
        # (still fine to end up None if that's never been set either — we never
        # invent a value). Extend here for finer-grained fallbacks later, e.g. a
        # per-customer override via a "default_payment_method:<customer_name>"
        # preference key, checked before the global one.
        if payment_method is None:
            payment_method = await get_preference_value(session, "default_payment_method")
        bill.payment_method = payment_method

        if on_credit:
            # Same session/transaction as everything above — this commit is the
            # only commit in the whole function, so a duplicate finalize_bill
            # call (blocked by the Bill row lock until this one commits) can
            # never see status=="draft" again and can never double-add to khata.
            await add_credit(session, bill.customer_name, float(bill.grand_total), related_bill_id=bill.id)

        await session.commit()
        await session.refresh(bill)
        credit_note = " (added to khata as credit)" if on_credit else ""
        payment_note = f" Payment method: {bill.payment_method}." if bill.payment_method else ""
        return (
            f"Bill {bill.id} finalized for {bill.customer_name or 'a walk-in customer'}. "
            f"Subtotal={bill.subtotal}, CGST={bill.cgst_total}, SGST={bill.sgst_total}, "
            f"Grand Total={bill.grand_total}{credit_note}.{payment_note}"
        )


ALL_TOOLS = [start_bill, add_item, edit_item, finalize_bill]
