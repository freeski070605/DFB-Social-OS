from sqlalchemy import String, Text, JSON, DateTime, ForeignKey, UniqueConstraint, Boolean
from sqlalchemy.orm import Mapped, mapped_column
from app.db.session import Base, utcnow


class Interaction(Base):
    __tablename__ = "interactions"
    __table_args__ = (UniqueConstraint("brand_id", "platform", "external_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    platform: Mapped[str] = mapped_column(String(30))
    external_id: Mapped[str] = mapped_column(String(200))
    thread_id: Mapped[str] = mapped_column(String(200), default="")
    author: Mapped[str] = mapped_column(String(200), default="")
    kind: Mapped[str] = mapped_column(String(20), default="comment")
    body: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(40), default="unknown")
    action: Mapped[str] = mapped_column(String(30), default="ADMIN_REVIEW")
    confidence: Mapped[float] = mapped_column(default=0)
    draft: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="NEW", index=True)
    taken_over: Mapped[bool] = mapped_column(Boolean, default=False)
    reply_id: Mapped[str] = mapped_column(String(200), default="")
    replied_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow)


class MetricSnapshot(Base):
    __tablename__ = "metric_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    content_id: Mapped[int | None] = mapped_column(ForeignKey("content.id"), nullable=True, index=True)
    platform: Mapped[str] = mapped_column(String(30))
    metrics: Mapped[dict] = mapped_column(JSON)
    dimensions: Mapped[dict] = mapped_column(JSON, default=dict)
    captured_at: Mapped[object] = mapped_column(DateTime, default=utcnow, index=True)


class StrategyChange(Base):
    __tablename__ = "strategy_changes"
    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    before: Mapped[dict] = mapped_column(JSON)
    after: Mapped[dict] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="PROPOSED")
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow)
