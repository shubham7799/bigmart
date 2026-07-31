from langchain_core.tools import tool
from sqlalchemy import func, select

from app.db.models import Customer, KhataEntry
from app.db.session import async_session_maker


async def _find_customer(session, name: str) -> Customer | None:
    stmt = select(Customer).where(func.lower(Customer.name) == name.strip().lower())
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _get_balance(session, customer_id: int) -> float:
    stmt = select(KhataEntry).where(KhataEntry.customer_id == customer_id)
    entries = (await session.execute(stmt)).scalars().all()
    credit = sum(float(e.amount) for e in entries if e.entry_type == "credit")
    payment = sum(float(e.amount) for e in entries if e.entry_type == "payment")
    return credit - payment


async def add_credit(
    session, customer_name: str, amount: float, related_bill_id: int | None = None
) -> Customer:
    """Core credit-add logic, factored out so it can run inside an *existing*
    session/transaction — used by both the standalone khata_add tool below and by
    billing_tools.finalize_bill's on_credit path. Sharing finalize_bill's already
    open session/lock scope means a duplicate finalize call (blocked by the Bill
    row lock from Phase 3) can never double-add to the khata: the caller commits,
    not this function."""
    customer = await _find_customer(session, customer_name)
    if customer is None:
        customer = Customer(name=customer_name)
        session.add(customer)
        await session.flush()

    session.add(
        KhataEntry(
            customer_id=customer.id,
            entry_type="credit",
            amount=amount,
            related_bill_id=related_bill_id,
        )
    )
    await session.flush()
    return customer


@tool
async def khata_add(customer_name: str, amount: float, related_bill_id: int | None = None) -> str:
    """Add a credit (amount the customer now owes) to a customer's khata ledger,
    creating the customer if they don't have one yet. Use for standalone credit
    entries not tied to finalizing a bill (finalize_bill's on_credit option
    handles that case automatically). Rejects amount <= 0."""
    if amount <= 0:
        return "amount must be positive."
    async with async_session_maker() as session:
        customer = await add_credit(session, customer_name, amount, related_bill_id)
        await session.commit()
        balance = await _get_balance(session, customer.id)
        return f"Added credit of {amount} for '{customer.name}'. Outstanding balance: {balance}."


@tool
async def khata_pay(customer_name: str, amount: float) -> str:
    """Record a payment against a customer's khata ledger, reducing what they owe.
    Rejects amount <= 0. Rejects if no ledger exists yet for that customer name
    (nothing to pay off). Rejects if amount exceeds the current outstanding
    balance (no overpaying past zero) — reports the real outstanding balance."""
    if amount <= 0:
        return "amount must be positive."
    async with async_session_maker() as session:
        customer = await _find_customer(session, customer_name)
        if customer is None:
            return f"No khata ledger exists for '{customer_name}' — nothing to pay off."

        balance = await _get_balance(session, customer.id)
        if amount > balance:
            return (
                f"Cannot record payment of {amount} for '{customer.name}' — outstanding "
                f"balance is only {balance}. That would overpay the ledger."
            )

        session.add(KhataEntry(customer_id=customer.id, entry_type="payment", amount=amount))
        await session.commit()
        return (
            f"Recorded payment of {amount} from '{customer.name}'. "
            f"New outstanding balance: {balance - amount}."
        )


@tool
async def khata_balance(customer_name: str) -> str:
    """Look up a customer's real outstanding khata balance (sum of credits minus
    payments), straight from the database. Reports if no ledger exists for that
    customer rather than returning 0."""
    async with async_session_maker() as session:
        customer = await _find_customer(session, customer_name)
        if customer is None:
            return f"No khata ledger exists for '{customer_name}'."
        balance = await _get_balance(session, customer.id)
        return f"'{customer.name}' outstanding khata balance: {balance}."


ALL_TOOLS = [khata_add, khata_pay, khata_balance]
