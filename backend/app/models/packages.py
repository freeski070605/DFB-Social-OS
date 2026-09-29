from sqlalchemy import DateTime, ForeignKey, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base, utcnow


class ContentPackage(Base):
    __tablename__ = "content_packages"
    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    topic: Mapped[str] = mapped_column(String(300))
    pillar: Mapped[str] = mapped_column(String(120))
    audience_promise: Mapped[str] = mapped_column(Text, default="")
    angle: Mapped[str] = mapped_column(Text, default="")
    knowledge_refs: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT")
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[object] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ContentDerivative(Base):
    __tablename__ = "content_derivatives"
    __table_args__ = (UniqueConstraint("package_id", "format", name="uq_package_format"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    package_id: Mapped[int] = mapped_column(ForeignKey("content_packages.id"), index=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    platform: Mapped[str] = mapped_column(String(30))
    format: Mapped[str] = mapped_column(String(40))
    content_id: Mapped[int | None] = mapped_column(ForeignKey("content.id"), nullable=True)
    content_revision: Mapped[int | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="REVIEW")
    plan: Mapped[dict] = mapped_column(JSON, default=dict)
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    used_knowledge_refs: Mapped[list] = mapped_column(JSON, default=list)
    generation: Mapped[dict] = mapped_column(JSON, default=dict)
    evaluation: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[object] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
