import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import String, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class TreeMergeRequest(Base):
    """
    Demande de fusion d'un arbre source dans un arbre cible.

    Flux :
      1. N'importe quel user authentifié soumet une demande (status=pending).
      2. Tout membre/propriétaire de l'arbre cible peut approuver ou rejeter.
      3. À l'approbation, converge_trees() est exécuté en mode système.
    """
    __tablename__ = "tree_merge_requests"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_tree_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("family_trees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_tree_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("family_trees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Fiche de l'auteur dans son arbre source (optionnel)
    source_person_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("persons.id", ondelete="SET NULL"), nullable=True
    )
    # Fiche correspondante dans l'arbre cible (optionnel)
    target_person_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("persons.id", ondelete="SET NULL"), nullable=True
    )
    requested_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    # pending | approved | rejected
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    reviewed_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Noms dénormalisés pour affichage sans JOIN (snapshot au moment de la demande)
    source_tree_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    target_tree_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    requester_first_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
