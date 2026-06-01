import uuid
from datetime import datetime
from typing import Optional
from sqlalchemy import String, DateTime, ForeignKey, func, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from app.database import Base
from app.security.types import EncryptedString


class Invitation(Base):
    __tablename__ = "invitations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Token URL-safe envoyé par lien (UUID v4)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    # Code court de validation (6 chiffres) envoyé par SMS à l'invité
    validation_code: Mapped[str] = mapped_column(String(8), nullable=False)
    # Numéro de téléphone de l'invité (chiffré) + hash pour la déduplication
    invited_phone: Mapped[str] = mapped_column(EncryptedString, nullable=False)
    invited_phone_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    # Qui a émis l'invitation
    inviter_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    # Statut : pending / validated / expired / revoked
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # SMS envoyé ?
    sms_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    validated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    inviter: Mapped["User"] = relationship("User", foreign_keys=[inviter_user_id])
