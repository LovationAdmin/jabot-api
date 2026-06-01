import uuid
from datetime import datetime
from typing import Optional
from sqlalchemy import String, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from app.database import Base
from app.security.types import EncryptedString


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Téléphone chiffré au repos. L'unicité et les recherches par égalité
    # passent par phone_hash (hash déterministe), car le ciphertext Fernet
    # n'est pas déterministe.
    phone: Mapped[str] = mapped_column(EncryptedString, nullable=False)
    phone_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    person_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("persons.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationship to linked person in tree
    person: Mapped[Optional["Person"]] = relationship(
        "Person", foreign_keys=[person_id], lazy="select"
    )
