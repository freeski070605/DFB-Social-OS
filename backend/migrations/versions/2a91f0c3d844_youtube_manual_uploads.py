"""Revision-bound YouTube video assets and resumable upload attempts.

Revision ID: 2a91f0c3d844
Revises: f1d400b0a332
"""
from alembic import op
import sqlalchemy as sa

revision = "2a91f0c3d844"
down_revision = "f1d400b0a332"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("youtube_video_assets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("brand_id", sa.Integer(), sa.ForeignKey("brands.id"), nullable=False),
        sa.Column("content_id", sa.Integer(), sa.ForeignKey("content.id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.String(500), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False))
    op.create_index("ix_youtube_video_assets_brand_id", "youtube_video_assets", ["brand_id"])
    op.create_index("ix_youtube_video_assets_content_id", "youtube_video_assets", ["content_id"])
    op.create_table("youtube_upload_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("brand_id", sa.Integer(), sa.ForeignKey("brands.id"), nullable=False),
        sa.Column("content_id", sa.Integer(), sa.ForeignKey("content.id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("asset_id", sa.Integer(), sa.ForeignKey("youtube_video_assets.id"), nullable=False),
        sa.Column("asset_sha256", sa.String(64), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("platform_accounts.id"), nullable=False),
        sa.Column("channel_id", sa.String(200), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("session_uri", sa.Text(), nullable=False),
        sa.Column("bytes_sent", sa.Integer(), nullable=False),
        sa.Column("provider_video_id", sa.String(200), nullable=False),
        sa.Column("upload_metadata", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_youtube_upload_idempotency"))
    op.create_index("ix_youtube_upload_attempts_brand_id", "youtube_upload_attempts", ["brand_id"])
    op.create_index("ix_youtube_upload_attempts_content_id", "youtube_upload_attempts", ["content_id"])
    op.create_index("ix_youtube_upload_attempts_state", "youtube_upload_attempts", ["state"])


def downgrade():
    op.drop_table("youtube_upload_attempts")
    op.drop_table("youtube_video_assets")