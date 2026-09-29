"""Add brand scoped content packages and native derivatives.

Revision ID: 84cbe0e4f7a1
Revises: 7c4b323505ad
"""
from alembic import op
import sqlalchemy as sa

revision = "84cbe0e4f7a1"
down_revision = "7c4b323505ad"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("content_packages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("brand_id", sa.Integer(), sa.ForeignKey("brands.id"), nullable=False),
        sa.Column("topic", sa.String(300), nullable=False),
        sa.Column("pillar", sa.String(120), nullable=False),
        sa.Column("audience_promise", sa.Text(), nullable=False),
        sa.Column("angle", sa.Text(), nullable=False),
        sa.Column("knowledge_refs", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False))
    op.create_index("ix_content_packages_brand_id", "content_packages", ["brand_id"])
    op.create_table("content_derivatives",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("package_id", sa.Integer(), sa.ForeignKey("content_packages.id"), nullable=False),
        sa.Column("brand_id", sa.Integer(), sa.ForeignKey("brands.id"), nullable=False),
        sa.Column("platform", sa.String(30), nullable=False),
        sa.Column("format", sa.String(40), nullable=False),
        sa.Column("content_id", sa.Integer(), sa.ForeignKey("content.id")),
        sa.Column("content_revision", sa.Integer()),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("plan", sa.JSON(), nullable=False),
        sa.Column("output", sa.JSON(), nullable=False),
        sa.Column("used_knowledge_refs", sa.JSON(), nullable=False),
        sa.Column("generation", sa.JSON(), nullable=False),
        sa.Column("evaluation", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("package_id", "format", name="uq_package_format"))
    op.create_index("ix_content_derivatives_brand_id", "content_derivatives", ["brand_id"])
    op.create_index("ix_content_derivatives_package_id", "content_derivatives", ["package_id"])


def downgrade():
    op.drop_table("content_derivatives")
    op.drop_table("content_packages")
