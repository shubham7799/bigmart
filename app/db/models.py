from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Numeric, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    sku: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
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
    checkpointer would otherwise store."""

    __tablename__ = "conversations"

    thread_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    messages: Mapped[list[Any]] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
