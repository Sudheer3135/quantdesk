"""Database tables."""
from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SignalRecord(Base):
    """Every signal the engine produces, saved whether or not it was traded.
    This is what makes the system auditable after the fact."""
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timeframe: Mapped[str] = mapped_column(String(8))
    action: Mapped[str] = mapped_column(String(8), index=True)
    confidence: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    entry: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    target: Mapped[float | None] = mapped_column(Float, nullable=True)
    checks: Mapped[dict] = mapped_column(JSON, default=dict)
    context: Mapped[dict] = mapped_column(JSON, default=dict)


class TradeRecord(Base):
    """The trade journal. Fill the review fields after the close, not during."""
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    symbol: Mapped[str] = mapped_column(String(48), index=True)
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[int] = mapped_column(Integer)
    entry: Mapped[float] = mapped_column(Float)
    exit: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float] = mapped_column(Float)
    target: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    r_multiple: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    signal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    setup: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plan_followed: Mapped[bool | None] = mapped_column(nullable=True)
    mistakes: Mapped[str | None] = mapped_column(Text, nullable=True)
    lesson: Mapped[str | None] = mapped_column(Text, nullable=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)


class CandleRecord(Base):
    """Archived candles.

    Free data sources cap how far back you can look. This table is how you
    beat that cap: every fetch is stored, so your own history grows from the
    day you switch it on. Nobody can revoke it or start charging for it.
    """
    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "timestamp", name="uq_candle"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timeframe: Mapped[str] = mapped_column(String(8), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(16), default="free")
