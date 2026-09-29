from sqlalchemy import String, Text, JSON, Boolean, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.db.session import Base, utcnow


class Brand(Base):
    __tablename__ = "brands"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    paused: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[object] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Admin(Base):
    __tablename__ = "admins"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow)


class AdminSession(Base):
    __tablename__ = "admin_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    admin_id: Mapped[int] = mapped_column(ForeignKey("admins.id"))
    csrf: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[object] = mapped_column(DateTime, index=True)


class SystemSetting(Base):
    __tablename__ = "system_settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)


class PlatformAccount(Base):
    __tablename__ = "platform_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    platform: Mapped[str] = mapped_column(String(30))
    account_id: Mapped[str] = mapped_column(String(200))
    token_encrypted: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)


class SocialIdentity(Base):
    """Owner-entered public identity, separate from provider-verified authorization."""
    __tablename__ = "social_identities"
    __table_args__ = (UniqueConstraint("brand_id", "platform", name="uq_social_identity_brand_platform"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    platform: Mapped[str] = mapped_column(String(30))
    display_name: Mapped[str] = mapped_column(String(120), default="")
    username: Mapped[str] = mapped_column(String(120), default="")
    updated_at: Mapped[object] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
