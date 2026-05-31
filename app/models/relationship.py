import uuid
from datetime import datetime
from sqlalchemy import String, DateTime, ForeignKey, func, UniqueConstraint, CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from app.database import Base


class Relationship(Base):
    __tablename__ = "relationships"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    person_a_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("persons.id"), nullable=False
    )
    person_b_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("persons.id"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(30), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    person_a: Mapped["Person"] = relationship(
        "Person", foreign_keys=[person_a_id], back_populates="relationships_as_a"
    )
    person_b: Mapped["Person"] = relationship(
        "Person", foreign_keys=[person_b_id], back_populates="relationships_as_b"
    )

    __table_args__ = (
        UniqueConstraint("person_a_id", "person_b_id", "type", name="uq_relationship"),
        CheckConstraint(
            "type IN ('parent', 'child', 'sibling', 'spouse', "
            "'half_sibling', 'step_sibling', 'step_parent', 'step_child', 'homonym', "
            "'grandparent', 'grandchild', 'uncle_aunt', 'nephew_niece', 'cousin')",
            name="chk_relationship_type"
        ),
    )
