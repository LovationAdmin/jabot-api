"""011_tree_merge_requests

Revision ID: 011
Revises: 010
Create Date: 2026-06-04
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tree_merge_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_tree_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("family_trees.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_tree_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("family_trees.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_person_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("persons.id", ondelete="SET NULL"), nullable=True),
        sa.Column("target_person_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("persons.id", ondelete="SET NULL"), nullable=True),
        sa.Column("requested_by_user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("reviewed_by_user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id"), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_tree_name", sa.String(255), nullable=True),
        sa.Column("target_tree_name", sa.String(255), nullable=True),
        sa.Column("requester_first_name", sa.String(255), nullable=True),
    )
    op.create_index("ix_tree_merge_requests_target_tree_id", "tree_merge_requests", ["target_tree_id"])
    op.create_index("ix_tree_merge_requests_source_tree_id", "tree_merge_requests", ["source_tree_id"])
    op.create_index("ix_tree_merge_requests_status", "tree_merge_requests", ["status"])


def downgrade() -> None:
    op.drop_table("tree_merge_requests")
