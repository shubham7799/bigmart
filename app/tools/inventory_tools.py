from langchain_core.tools import tool
from sqlalchemy import select

from app.db.models import Product, StockTxn
from app.db.session import async_session_maker


async def _find_products(session, name: str) -> list[Product]:
    stmt = select(Product).where(Product.name.ilike(f"%{name}%"))
    result = await session.execute(stmt)
    return list(result.scalars().all())


def _ambiguous_message(matches: list[Product]) -> str:
    lines = [
        f"- id={p.id}, name='{p.name}', unit={p.unit}, quantity_on_hand={p.quantity_on_hand}"
        for p in matches
    ]
    return "Multiple products match that name, ask the user which one they mean:\n" + "\n".join(lines)


@tool
async def add_product(
    name: str,
    unit: str,
    gst_slab: float,
    hsn_code: str,
    cost_price: float,
    sale_price: float,
) -> str:
    """Create a new product in the inventory. Use only when the user wants to define
    a brand-new product with full details, not just add stock to an existing one."""
    async with async_session_maker() as session:
        existing = await _find_products(session, name)
        exact = [p for p in existing if p.name.lower() == name.lower()]
        if exact:
            return (
                f"Product '{exact[0].name}' already exists (id={exact[0].id}). "
                "Use receive_stock to add quantity instead."
            )

        product = Product(
            name=name,
            unit=unit,
            gst_slab=gst_slab,
            hsn_code=hsn_code,
            cost_price=cost_price,
            sale_price=sale_price,
            quantity_on_hand=0,
        )
        session.add(product)
        await session.commit()
        await session.refresh(product)
        return (
            f"Created product '{product.name}' (id={product.id}), unit={product.unit}, "
            f"cost_price={product.cost_price}, sale_price={product.sale_price}."
        )


@tool
async def receive_stock(product_name: str, quantity: float, cost_price: float | None = None) -> str:
    """Record stock received for a product, increasing its quantity_on_hand and
    logging a StockTxn. If the product doesn't exist yet, it is created automatically
    using product_name and cost_price (unit defaults to 'pc'; call add_product
    afterward to set gst_slab/hsn_code/sale_price precisely). If product_name matches
    multiple existing products, returns the list of matches instead of guessing."""
    async with async_session_maker() as session:
        matches = await _find_products(session, product_name)
        if len(matches) > 1:
            return _ambiguous_message(matches)

        if len(matches) == 1:
            product = matches[0]
            if cost_price is not None:
                product.cost_price = cost_price
        else:
            product = Product(
                name=product_name,
                unit="pc",
                gst_slab=0,
                hsn_code="",
                cost_price=cost_price or 0,
                sale_price=cost_price or 0,
                quantity_on_hand=0,
            )
            session.add(product)
            await session.flush()

        txn = StockTxn(product_id=product.id, change_qty=quantity, reason="received")
        session.add(txn)
        product.quantity_on_hand = float(product.quantity_on_hand) + quantity

        await session.commit()
        await session.refresh(product)
        return (
            f"Received {quantity} {product.unit} of '{product.name}'. "
            f"New quantity_on_hand={product.quantity_on_hand}."
        )


@tool
async def get_stock(product_name: str) -> str:
    """Look up the current quantity_on_hand for a product by name, straight from the
    database. If product_name matches multiple products, returns the list of matches
    instead of guessing so you can ask the user to clarify."""
    async with async_session_maker() as session:
        matches = await _find_products(session, product_name)
        if not matches:
            return f"No product found matching '{product_name}'."
        if len(matches) > 1:
            return _ambiguous_message(matches)

        product = matches[0]
        return f"'{product.name}' (id={product.id}): quantity_on_hand={product.quantity_on_hand} {product.unit}."


ALL_TOOLS = [add_product, receive_stock, get_stock]
