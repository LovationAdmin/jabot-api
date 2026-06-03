import uuid
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, func, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID
from app.database import Base


class IgnoredDuplicate(Base):
    """
    Paire de personnes qu'un membre de l'arbre a explicitement declaree comme
    n'etant PAS un doublon. L'ignore est partage par tout l'arbre (peu importe
    l'utilisateur/la session) : la paire ne sera plus proposee a l'examen.

    Les deux ids sont stockes dans un ordre normalise (person_low < person_high)
    pour que la paire soit independante du sens (a,b) == (b,a).
    """

    __tablename__ = "ignored_duplicates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    family_tree_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("family_trees.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    person_low_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("persons.id", ondelete="CASCADE"), nullable=False
    )
    person_high_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("persons.id", ondelete="CASCADE"), nullable=False
    )
    ignored_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "family_tree_id", "person_low_id", "person_high_id",
            name="uq_ignored_duplicate_pair",
        ),
    )
