"""Seed default tree and backfill family_tree_id everywhere

Revision ID: 009
Revises: 008
Create Date: 2026-06-02

Folds ALL existing data into one default family tree ("Famille Jabot") so the
app keeps working unchanged after the multi-tree migration:
  - creates the default tree (oldest user = owner)
  - adds persons.family_tree_id / relationships.family_tree_id, backfills, NOT NULL
  - grants every existing user access (oldest = owner, others = member)
  - backfills invitations.family_tree_id
  - swaps the relationships unique constraint to be per-tree
"""
import uuid
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Add the columns as NULLABLE first (so existing rows don't violate NOT NULL)
    op.add_column("persons", sa.Column(
        "family_tree_id", UUID(as_uuid=True),
        sa.ForeignKey("family_trees.id", ondelete="CASCADE"), nullable=True))
    op.add_column("relationships", sa.Column(
        "family_tree_id", UUID(as_uuid=True),
        sa.ForeignKey("family_trees.id", ondelete="CASCADE"), nullable=True))

    # 2. If there is existing data, create the default tree and backfill.
    has_persons = conn.execute(sa.text("SELECT EXISTS(SELECT 1 FROM persons)")).scalar()
    has_users = conn.execute(sa.text("SELECT EXISTS(SELECT 1 FROM users)")).scalar()

    if has_persons or has_users:
        default_tree_id = uuid.uuid4()
        oldest_user_id = conn.execute(
            sa.text("SELECT id FROM users ORDER BY created_at ASC LIMIT 1")
        ).scalar()

        conn.execute(
            sa.text("""
                INSERT INTO family_trees (id, name, created_by_user_id, created_at)
                VALUES (:tid, 'Famille Jabot', :uid, NOW())
            """),
            {"tid": default_tree_id, "uid": oldest_user_id},
        )

        # Backfill persons and relationships
        conn.execute(
            sa.text("UPDATE persons SET family_tree_id = :tid WHERE family_tree_id IS NULL"),
            {"tid": default_tree_id},
        )
        conn.execute(
            sa.text("""
                UPDATE relationships r
                SET family_tree_id = p.family_tree_id
                FROM persons p
                WHERE p.id = r.person_a_id AND r.family_tree_id IS NULL
            """)
        )
        # Any relationship whose person_a was somehow missing → default tree
        conn.execute(
            sa.text("UPDATE relationships SET family_tree_id = :tid WHERE family_tree_id IS NULL"),
            {"tid": default_tree_id},
        )

        # Grant every existing user access (oldest = owner, others = member)
        conn.execute(
            sa.text("""
                INSERT INTO user_tree_access (user_id, family_tree_id, role, created_at)
                SELECT u.id, :tid,
                       CASE WHEN u.id = :owner THEN 'owner' ELSE 'member' END,
                       NOW()
                FROM users u
                ON CONFLICT (user_id, family_tree_id) DO NOTHING
            """),
            {"tid": default_tree_id, "owner": oldest_user_id},
        )

        # Backfill invitations
        conn.execute(
            sa.text("UPDATE invitations SET family_tree_id = :tid WHERE family_tree_id IS NULL"),
            {"tid": default_tree_id},
        )

    # 3. Enforce NOT NULL now that everything is backfilled.
    op.alter_column("persons", "family_tree_id", nullable=False)
    op.alter_column("relationships", "family_tree_id", nullable=False)

    # 4. Swap the relationships unique constraint to be per-tree.
    op.drop_constraint("uq_relationship", "relationships", type_="unique")
    op.create_unique_constraint(
        "uq_relationship_per_tree", "relationships",
        ["family_tree_id", "person_a_id", "person_b_id", "type"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_relationship_per_tree", "relationships", type_="unique")
    op.create_unique_constraint(
        "uq_relationship", "relationships",
        ["person_a_id", "person_b_id", "type"],
    )
    op.drop_column("relationships", "family_tree_id")
    op.drop_column("persons", "family_tree_id")
    # The default family_trees row + user_tree_access rows are left in place;
    # migration 008's downgrade drops those tables entirely if needed.
