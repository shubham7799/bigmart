from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Multi-tenancy: shop_id is the Telegram chat_id (as a string) of the shop
# owner's chat with the bot — the same value already used as Conversation's
# thread_id, just reused as the tenant key for actual business data too. It is
# always derived server-side from the incoming Telegram update (see
# app/telegram/webhook.py -> app/agent/runtime.py), never supplied by the
# model — every tool takes it as an InjectedToolArg (hidden from the LLM's
# function-calling schema) rather than a normal argument the model could set.


class Base(DeclarativeBase):
    pass


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    # Not currently set by any tool (vestigial from before multi-tenancy) — not
    # globally unique since two different shops may reuse the same SKU. A real
    # per-shop uniqueness constraint would need a composite (shop_id, sku).
    sku: Mapped[str | None] = mapped_column(String(64), nullable=True)
    unit: Mapped[str] = mapped_column(String(16))
    gst_slab: Mapped[float] = mapped_column(Numeric(5, 2), default=0)
    hsn_code: Mapped[str] = mapped_column(String(16), default="")
    cost_price: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    sale_price: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    quantity_on_hand: Mapped[float] = mapped_column(Numeric(12, 3), default=0)

    stock_txns: Mapped[list["StockTxn"]] = relationship(back_populates="product")


class StockTxn(Base):
    __tablename__ = "stock_txns"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    change_qty: Mapped[float] = mapped_column(Numeric(12, 3))
    reason: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    product: Mapped["Product"] = relationship(back_populates="stock_txns")


class Bill(Base):
    __tablename__ = "bills"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[str] = mapped_column(String(64), index=True)
    customer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="draft")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    subtotal: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    cgst_total: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    sgst_total: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    grand_total: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    payment_method: Mapped[str | None] = mapped_column(String(32), nullable=True)

    items: Mapped[list["BillItem"]] = relationship(back_populates="bill")


class BillItem(Base):
    __tablename__ = "bill_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    bill_id: Mapped[int] = mapped_column(ForeignKey("bills.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    quantity: Mapped[float] = mapped_column(Numeric(12, 3))
    unit_price: Mapped[float] = mapped_column(Numeric(12, 2))
    gst_slab: Mapped[float] = mapped_column(Numeric(5, 2))
    line_subtotal: Mapped[float] = mapped_column(Numeric(12, 2))
    line_cgst: Mapped[float] = mapped_column(Numeric(12, 2))
    line_sgst: Mapped[float] = mapped_column(Numeric(12, 2))
    line_total: Mapped[float] = mapped_column(Numeric(12, 2))

    bill: Mapped["Bill"] = relationship(back_populates="items")
    product: Mapped["Product"] = relationship()


class Customer(Base):
    """Customer identity is matched by exact (case-insensitive) name only,
    *within a shop* — no phone-based dedup or fuzzy matching. Two different
    real people with the same name at the same shop will collide onto the
    same khata ledger; a real fix needs an actual customer lookup/dedup flow,
    which is out of scope here."""

    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)


class KhataEntry(Base):
    __tablename__ = "khata_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    entry_type: Mapped[str] = mapped_column(String(16))  # "credit" or "payment"
    amount: Mapped[float] = mapped_column(Numeric(12, 2))
    related_bill_id: Mapped[int | None] = mapped_column(ForeignKey("bills.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    customer: Mapped["Customer"] = relationship()


class Preference(Base):
    """Standing shop-level settings (shop name, GSTIN, default payment method,
    preferred brand per category, etc). Deliberately a flexible key/value shape
    rather than fixed columns, since the set of preferences will keep growing
    and a fixed-column table would need a migration for every new one. Scoped
    per shop_id (each shop has its own "shop_name", "gstin", etc — not shared
    globally across shops)."""

    __tablename__ = "preferences"
    __table_args__ = (UniqueConstraint("shop_id", "key", name="uq_preferences_shop_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[str] = mapped_column(String(64), index=True)
    key: Mapped[str] = mapped_column(String(128), index=True)
    value: Mapped[str] = mapped_column(String(1024))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class ProcessedUpdate(Base):
    """Records Telegram update_ids we've already handled, so a duplicate webhook
    delivery of the same update can be detected and skipped *before* the agent
    (and any tool side effects) runs. The primary key doubles as the uniqueness
    constraint: a single INSERT that violates it is how two near-simultaneous
    deliveries of the same update are kept from both passing the check — see
    app/telegram/webhook.py."""

    __tablename__ = "processed_updates"

    id: Mapped[int] = mapped_column(primary_key=True)  # Telegram update_id
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class Conversation(Base):
    """Per-thread chat history, so the agent remembers context (e.g. the active
    bill_id) across turns and across server restarts — replaces what a langgraph
    checkpointer would otherwise store. thread_id is the same Telegram chat_id
    used as shop_id elsewhere (see the module docstring above) — one Telegram
    chat is one shop's conversation AND one shop's tenant key."""

    __tablename__ = "conversations"

    thread_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    messages: Mapped[list[Any]] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
