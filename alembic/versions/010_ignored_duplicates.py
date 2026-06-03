"""Ignored duplicates: tree-wide dismissed duplicate pairs

Revision ID: 010
Revises: 009
Create Date: 2026-06-03

Additive only — stores pairs of persons that a tree member declared as NOT
duplicates. The dismissal is shared across the whole tree (any user/session).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ignored_duplicates",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("family_tree_id", UUID(as_uuid=True),
                  sa.ForeignKey("family_trees.id", ondelete="CASCADE"), nullable=False),
        sa.Column("person_low_id", UUID(as_uuid=True),
                  sa.ForeignKey("persons.id", ondelete="CASCADE"), nullable=False),
        sa.Column("person_high_id", UUID(as_uuid=True),
                  sa.ForeignKey("persons.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ignored_by", UUID(as_uuid=True),
                  sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("family_tree_id", "person_low_id", "person_high_id",
                            name="uq_ignored_duplicate_pair"),
    )
    op.create_index("ix_ignored_duplicates_tree", "ignored_duplicates", ["family_tree_id"])


def downgrade() -> None:
    op.drop_index("ix_ignored_duplicates_tree", table_name="ignored_duplicates")
    op.drop_table("ignored_duplicates")
