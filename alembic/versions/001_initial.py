"""Initial schema with pg_trgm + fuzzystrmatch extensions and all tables.

Revision ID: 001
Revises:
Create Date: 2024-01-01 00:00:00.000000
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, ARRAY, TEXT

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Enable PostgreSQL extensions
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS fuzzystrmatch")
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')

    # --- persons table (created first, no FKs to other tables yet) ---
    op.create_table(
        "persons",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("first_name", sa.String(100), nullable=False),
        sa.Column("last_name", sa.String(100), nullable=True),
        sa.Column("nicknames", ARRAY(TEXT), nullable=True),
        sa.Column("gender", sa.String(10), nullable=True),
        sa.Column("birth_date", sa.Date, nullable=True),
        sa.Column("death_date", sa.Date, nullable=True),
        sa.Column("city_of_origin", sa.String(200), nullable=True),
        sa.Column("created_by", UUID(as_uuid=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
    )

    # --- users table ---
    op.create_table(
        "users",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("phone", sa.String(20), unique=True, nullable=False),
        sa.Column("person_id", UUID(as_uuid=True), sa.ForeignKey("persons.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
    )

    # Now add created_by FK to persons
    op.create_foreign_key(
        "fk_persons_created_by",
        "persons", "users",
        ["created_by"], ["id"],
    )

    # --- canvas_positions table ---
    op.create_table(
        "canvas_positions",
        sa.Column("person_id", UUID(as_uuid=True), sa.ForeignKey("persons.id"), primary_key=True),
        sa.Column("x", sa.Float, nullable=False, server_default="0"),
        sa.Column("y", sa.Float, nullable=False, server_default="0"),
        sa.Column("generation", sa.Integer, server_default="0"),
    )

    # --- relationships table ---
    op.create_table(
        "relationships",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("person_a_id", UUID(as_uuid=True), sa.ForeignKey("persons.id"), nullable=False),
        sa.Column("person_b_id", UUID(as_uuid=True), sa.ForeignKey("persons.id"), nullable=False),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.UniqueConstraint("person_a_id", "person_b_id", "type", name="uq_relationship"),
        sa.CheckConstraint(
            "type IN ('parent', 'child', 'sibling', 'spouse')",
            name="chk_relationship_type",
        ),
    )

    # --- media table ---
    op.create_table(
        "media",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("person_id", UUID(as_uuid=True), sa.ForeignKey("persons.id"), nullable=False),
        sa.Column("type", sa.String(10), nullable=False),
        sa.Column("cloudinary_id", sa.String(500), nullable=False),
        sa.Column("url", sa.String(1000), nullable=False),
        sa.Column("duration_seconds", sa.Integer, nullable=True),
        sa.Column("file_size_bytes", sa.BigInteger, nullable=True),
        sa.Column("order_index", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.CheckConstraint("type IN ('photo', 'audio')", name="chk_media_type"),
    )

    # --- Trigram indexes ---
    op.execute(
        "CREATE INDEX idx_persons_first_name_trgm ON persons USING GIN (first_name gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX idx_persons_last_name_trgm ON persons USING GIN (last_name gin_trgm_ops) "
        "WHERE last_name IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX idx_persons_nicknames ON persons USING GIN (nicknames) "
        "WHERE nicknames IS NOT NULL"
    )

    # Regular indexes
    op.create_index("idx_persons_deleted_at", "persons", ["deleted_at"])
    op.create_index("idx_media_person_id", "media", ["person_id"])
    op.create_index("idx_relationships_person_a", "relationships", ["person_a_id"])
    op.create_index("idx_relationships_person_b", "relationships", ["person_b_id"])


def downgrade() -> None:
    op.drop_index("idx_relationships_person_b", table_name="relationships")
    op.drop_index("idx_relationships_person_a", table_name="relationships")
    op.drop_index("idx_media_person_id", table_name="media")
    op.drop_index("idx_persons_deleted_at", table_name="persons")
    op.execute("DROP INDEX IF EXISTS idx_persons_nicknames")
    op.execute("DROP INDEX IF EXISTS idx_persons_last_name_trgm")
    op.execute("DROP INDEX IF EXISTS idx_persons_first_name_trgm")
    op.drop_table("media")
    op.drop_table("relationships")
    op.drop_table("canvas_positions")
    op.drop_constraint("fk_persons_created_by", "persons", type_="foreignkey")
    op.drop_table("users")
    op.drop_table("persons")
