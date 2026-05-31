"""Extend relationship types: add grandparent, grandchild, uncle_aunt, nephew_niece, cousin.

Revision ID: 004
Revises: 003
Create Date: 2026-05-31 00:00:00.000000
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ALL_TYPES = (
    "'parent', 'child', 'sibling', 'spouse', "
    "'half_sibling', 'step_sibling', 'step_parent', 'step_child', 'homonym', "
    "'grandparent', 'grandchild', 'uncle_aunt', 'nephew_niece', 'cousin'"
)


def upgrade() -> None:
    op.drop_constraint("chk_relationship_type", "relationships", type_="check")
    op.create_check_constraint(
        "chk_relationship_type",
        "relationships",
        f"type IN ({ALL_TYPES})",
    )


def downgrade() -> None:
    op.drop_constraint("chk_relationship_type", "relationships", type_="check")
    op.create_check_constraint(
        "chk_relationship_type",
        "relationships",
        "type IN ('parent', 'child', 'sibling', 'spouse', "
        "'half_sibling', 'step_sibling', 'step_parent', 'step_child', 'homonym')",
    )
