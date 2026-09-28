from sqlalchemy import String, Text, JSON, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.db.session import Base, utcnow


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    target_id: Mapped[int | None] = mapped_column(nullable=True)
    key: Mapped[str] = mapped_column(String(200), unique=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", index=True)
    run_at: Mapped[object] = mapped_column(DateTime, index=True)
    attempts: Mapped[int] = mapped_column(default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[object] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Publication(Base):
    __tablename__ = "publications"
    __table_args__ = (UniqueConstraint("content_id", "platform"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    content_id: Mapped[int] = mapped_column(ForeignKey("content.id"), index=True)
    platform: Mapped[str] = mapped_column(String(30))
    external_id: Mapped[str] = mapped_column(String(200), default="")
    state: Mapped[str] = mapped_column(String(30), default="READY")
    attempts: Mapped[int] = mapped_column(default=0)
    request_state: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[object] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    action: Mapped[str] = mapped_column(String(40))
    target_id: Mapped[int] = mapped_column()
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow)
    decided_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)


class Audit(Base):
    __tablename__ = "audit"
    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int | None] = mapped_column(ForeignKey("brands.id"), nullable=True, index=True)
    actor: Mapped[str] = mapped_column(String(120))
    actor_type: Mapped[str] = mapped_column(String(20))
    action: Mapped[str] = mapped_column(String(100))
    target: Mapped[str] = mapped_column(String(120), default="")
    before: Mapped[dict] = mapped_column(JSON, default=dict)
    after: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[str] = mapped_column(String(40), default="SUCCESS")
    reason: Mapped[str] = mapped_column(Text, default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow, index=True)
