"""Helpers pour la gestion des arbres et des accès utilisateur↔arbre."""
import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.family_tree import FamilyTree, UserTreeAccess


async def create_tree(db: AsyncSession, name: str, owner_user_id: uuid.UUID) -> FamilyTree:
    """Crée un arbre et donne le rôle 'owner' à son créateur."""
    tree = FamilyTree(id=uuid.uuid4(), name=name or "Mon arbre", created_by_user_id=owner_user_id)
    db.add(tree)
    db.add(UserTreeAccess(user_id=owner_user_id, family_tree_id=tree.id, role="owner"))
    await db.flush()
    return tree


async def grant_access(
    db: AsyncSession, user_id: uuid.UUID, tree_id: uuid.UUID, role: str
) -> None:
    """Ajoute (ou met à niveau) l'accès d'un utilisateur à un arbre.

    Ne rétrograde jamais : si l'utilisateur est déjà owner, un grant 'member'
    ou 'visitor' est ignoré. owner > member > visitor.
    """
    rank = {"visitor": 0, "member": 1, "owner": 2}
    res = await db.execute(
        select(UserTreeAccess).where(
            UserTreeAccess.user_id == user_id,
            UserTreeAccess.family_tree_id == tree_id,
        )
    )
    existing = res.scalar_one_or_none()
    if existing is None:
        db.add(UserTreeAccess(user_id=user_id, family_tree_id=tree_id, role=role))
        return
    if rank.get(role, 0) > rank.get(existing.role, 0):
        existing.role = role


async def get_role(db: AsyncSession, user_id: uuid.UUID, tree_id: uuid.UUID) -> Optional[str]:
    res = await db.execute(
        select(UserTreeAccess.role).where(
            UserTreeAccess.user_id == user_id,
            UserTreeAccess.family_tree_id == tree_id,
        )
    )
    return res.scalar_one_or_none()


async def list_user_trees(db: AsyncSession, user_id: uuid.UUID) -> list[dict]:
    """Liste les arbres auxquels l'utilisateur a accès, avec rôle et nom."""
    res = await db.execute(
        select(FamilyTree.id, FamilyTree.name, UserTreeAccess.role, UserTreeAccess.created_at)
        .join(UserTreeAccess, UserTreeAccess.family_tree_id == FamilyTree.id)
        .where(UserTreeAccess.user_id == user_id)
        .order_by(UserTreeAccess.created_at.asc())
    )
    return [
        {"tree_id": str(r[0]), "tree_name": r[1], "role": r[2], "created_at": r[3].isoformat() if r[3] else None}
        for r in res.all()
    ]
