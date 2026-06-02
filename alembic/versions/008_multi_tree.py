"""Multi-tree: family_trees + user_tree_access tables

Revision ID: 008
Revises: 007
Create Date: 2026-06-02

Additive only — creates the new tables and adds a nullable family_tree_id to
invitations. Existing persons/relationships are NOT touched here; the data
backfill happens in migration 009.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "family_trees",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False, server_default="Mon arbre"),
        sa.Column("created_by_user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_family_trees_created_by", "family_trees", ["created_by_user_id"])

    op.create_table(
        "user_tree_access",
        sa.Column("user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("family_tree_id", UUID(as_uuid=True),
                  sa.ForeignKey("family_trees.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("role", sa.String(16), nullable=False, server_default="member"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("role IN ('owner', 'member', 'visitor')", name="chk_tree_role"),
    )
    op.create_index("ix_uta_user_id", "user_tree_access", ["user_id"])
    op.create_index("ix_uta_family_tree_id", "user_tree_access", ["family_tree_id"])

    # Invitations now point to the tree the invitee gets access to. Nullable for
    # backward compatibility; backfilled in migration 009.
    op.add_column("invitations", sa.Column(
        "family_tree_id", UUID(as_uuid=True),
        sa.ForeignKey("family_trees.id", ondelete="CASCADE"), nullable=True))


def downgrade() -> None:
    op.drop_column("invitations", "family_tree_id")
    op.drop_index("ix_uta_family_tree_id", table_name="user_tree_access")
    op.drop_index("ix_uta_user_id", table_name="user_tree_access")
    op.drop_table("user_tree_access")
    op.drop_index("ix_family_trees_created_by", table_name="family_trees")
    op.drop_table("family_trees")
