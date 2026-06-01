import uuid
from datetime import datetime, date
from typing import Optional, List
from sqlalchemy import (
    String, Date, DateTime, Float, Integer, ForeignKey, func, ARRAY, Text
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from app.database import Base
from app.security.types import EncryptedString, EncryptedDate


class Person(Base):
    __tablename__ = "persons"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    nicknames: Mapped[Optional[List[str]]] = mapped_column(ARRAY(Text), nullable=True)
    gender: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    # Champs chiffrés au repos (non utilisés par la recherche au niveau SQL ;
    # les boosts Python opèrent sur les valeurs déchiffrées transparentes).
    birth_date: Mapped[Optional[date]] = mapped_column(EncryptedDate, nullable=True)
    death_date: Mapped[Optional[date]] = mapped_column(EncryptedDate, nullable=True)
    city_of_origin: Mapped[Optional[str]] = mapped_column(EncryptedString, nullable=True)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    canvas_position: Mapped[Optional["CanvasPosition"]] = relationship(
        "CanvasPosition", back_populates="person", uselist=False, lazy="select"
    )
    media: Mapped[List["Media"]] = relationship(
        "Media", back_populates="person", lazy="select"
    )
    relationships_as_a: Mapped[List["Relationship"]] = relationship(
        "Relationship", foreign_keys="Relationship.person_a_id",
        back_populates="person_a", lazy="select"
    )
    relationships_as_b: Mapped[List["Relationship"]] = relationship(
        "Relationship", foreign_keys="Relationship.person_b_id",
        back_populates="person_b", lazy="select"
    )


class CanvasPosition(Base):
    __tablename__ = "canvas_positions"

    person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("persons.id"), primary_key=True
    )
    x: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    y: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    generation: Mapped[int] = mapped_column(Integer, default=0)

    # Relationship
    person: Mapped["Person"] = relationship("Person", back_populates="canvas_position")
