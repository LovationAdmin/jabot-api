"""Add invitations table

Revision ID: 006
Revises: 005
Create Date: 2026-06-01
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invitations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("token", sa.String(64), nullable=False, unique=True),
        sa.Column("validation_code", sa.String(8), nullable=False),
        sa.Column("invited_phone", sa.Text(), nullable=False),
        sa.Column("invited_phone_hash", sa.String(64), nullable=False),
        sa.Column("inviter_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("sms_sent", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_invitations_token", "invitations", ["token"], unique=True)
    op.create_index("ix_invitations_invited_phone_hash", "invitations", ["invited_phone_hash"])


def downgrade() -> None:
    op.drop_index("ix_invitations_invited_phone_hash", table_name="invitations")
    op.drop_index("ix_invitations_token", table_name="invitations")
    op.drop_table("invitations")
