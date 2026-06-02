"""add uploaded_by_user_id to media

Revision ID: 007
Revises: 006
Create Date: 2026-06-02
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = '007'
down_revision = '006'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('media', sa.Column(
        'uploaded_by_user_id',
        UUID(as_uuid=True),
        sa.ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
    ))


def downgrade():
    op.drop_column('media', 'uploaded_by_user_id')
