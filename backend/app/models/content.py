from sqlalchemy import String, Text, JSON, Boolean, DateTime, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column
from app.db.session import Base, utcnow


class Knowledge(Base):
    __tablename__ = "knowledge"
    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    category: Mapped[str] = mapped_column(String(120), index=True)
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, default="")
    verification: Mapped[str] = mapped_column(String(20), default="PENDING")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    restrictions: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[object] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Content(Base):
    __tablename__ = "content"
    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    topic: Mapped[str] = mapped_column(String(300))
    pillar: Mapped[str] = mapped_column(String(120))
    format: Mapped[str] = mapped_column(String(40), default="carousel")
    hook: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    slides: Mapped[list] = mapped_column(JSON, default=list)
    caption: Mapped[str] = mapped_column(Text, default="")
    cta: Mapped[str] = mapped_column(Text, default="")
    hashtags: Mapped[list] = mapped_column(JSON, default=list)
    knowledge_refs: Mapped[list] = mapped_column(JSON, default=list)
    sources: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", index=True)
    quality: Mapped[dict] = mapped_column(JSON, default=dict)
    targets: Mapped[list] = mapped_column(JSON, default=lambda: ["manual"])
    generation: Mapped[dict] = mapped_column(JSON, default=dict)
    assets: Mapped[list] = mapped_column(JSON, default=list)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("content.id"), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    scheduled_at: Mapped[object | None] = mapped_column(DateTime, nullable=True, index=True)
    published_at: Mapped[object | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[object] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Template(Base):
    __tablename__ = "templates"
    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(40))
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
