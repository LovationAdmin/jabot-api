import uuid
from datetime import datetime
from typing import Optional
from sqlalchemy import String, DateTime, ForeignKey, func, CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from app.database import Base


class FamilyTree(Base):
    """Un arbre généalogique = un espace isolé (multi-tenant).

    Chaque personne, relation et accès utilisateur appartient à exactement
    un arbre. Les arbres sont étanches : aucune requête ne doit traverser la
    frontière `family_tree_id`.
    """
    __tablename__ = "family_trees"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="Mon arbre")
    created_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UserTreeAccess(Base):
    """Lien d'accès entre un utilisateur et un arbre, avec un rôle.

    Rôles :
      - owner   : a créé l'arbre ; peut le renommer/supprimer, gérer les membres
      - member  : a une fiche dans l'arbre ; peut tout éditer (cards + liens)
      - visitor : invité ; lecture seule
    """
    __tablename__ = "user_tree_access"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    family_tree_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("family_trees.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("role IN ('owner', 'member', 'visitor')", name="chk_tree_role"),
    )
