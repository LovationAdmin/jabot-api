"""Extend relationship types with half_sibling, step_sibling, step_parent, step_child, homonym.

Revision ID: 002
Revises: 001
Create Date: 2026-05-31 00:00:00.000000
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Widen the column to accommodate longer type names (step_sibling = 12 chars)
    op.alter_column(
        "relationships",
        "type",
        existing_type=sa.String(20),
        type_=sa.String(30),
        existing_nullable=False,
    )

    # Drop the old check constraint and replace with the extended one
    op.drop_constraint("chk_relationship_type", "relationships", type_="check")
    op.create_check_constraint(
        "chk_relationship_type",
        "relationships",
        "type IN ('parent', 'child', 'sibling', 'spouse', "
        "'half_sibling', 'step_sibling', 'step_parent', 'step_child', 'homonym')",
    )


def downgrade() -> None:
    op.drop_constraint("chk_relationship_type", "relationships", type_="check")
    op.create_check_constraint(
        "chk_relationship_type",
        "relationships",
        "type IN ('parent', 'child', 'sibling', 'spouse')",
    )
    op.alter_column(
        "relationships",
        "type",
        existing_type=sa.String(30),
        type_=sa.String(20),
        existing_nullable=False,
    )
