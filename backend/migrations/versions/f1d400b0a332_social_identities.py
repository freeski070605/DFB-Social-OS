"""Manual brand social identities without credentials.

Revision ID: f1d400b0a332
Revises: 84cbe0e4f7a1
"""
from alembic import op
import sqlalchemy as sa

revision = "f1d400b0a332"
down_revision = "84cbe0e4f7a1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("social_identities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("brand_id", sa.Integer(), sa.ForeignKey("brands.id"), nullable=False),
        sa.Column("platform", sa.String(30), nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("username", sa.String(120), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("brand_id", "platform", name="uq_social_identity_brand_platform"))
    op.create_index("ix_social_identities_brand_id", "social_identities", ["brand_id"])


def downgrade():
    op.drop_table("social_identities")
