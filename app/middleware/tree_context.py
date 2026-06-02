"""
Résolution de l'arbre actif pour une requête (multi-tenant).

Chaque endpoint scopé lit l'arbre cible depuis le header `X-Tree-ID` (ou le
query param `?tree_id=`), vérifie que l'utilisateur y a accès, et expose le
rôle (`owner` / `member` / `visitor`).

Repli non-bloquant (Phase 1) : si aucun arbre n'est précisé, on retombe sur
le premier arbre de l'utilisateur, sinon sur l'arbre par défaut le plus ancien
(préserve le comportement « arbre global » d'avant le multi-arbre).
"""
import uuid
from typing import NamedTuple, Optional

from fastapi import Depends, Header, Query, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.family_tree import FamilyTree, UserTreeAccess
from app.models.user import User
from app.middleware.auth import get_current_user, get_current_user_optional


class TreeContext(NamedTuple):
    tree_id: uuid.UUID
    role: str  # 'owner' | 'member' | 'visitor'


def _parse_tid(x_tree_id: Optional[str], tree_id_param: Optional[uuid.UUID]) -> Optional[uuid.UUID]:
    if tree_id_param is not None:
        return tree_id_param
    if x_tree_id:
        try:
            return uuid.UUID(x_tree_id)
        except ValueError:
            return None
    return None


async def _role_in_tree(db: AsyncSession, user_id: uuid.UUID, tree_id: uuid.UUID) -> Optional[str]:
    res = await db.execute(
        select(UserTreeAccess.role).where(
            UserTreeAccess.user_id == user_id,
            UserTreeAccess.family_tree_id == tree_id,
        )
    )
    return res.scalar_one_or_none()


async def _first_tree_for_user(db: AsyncSession, user_id: uuid.UUID) -> Optional[TreeContext]:
    res = await db.execute(
        select(UserTreeAccess.family_tree_id, UserTreeAccess.role)
        .where(UserTreeAccess.user_id == user_id)
        .order_by(UserTreeAccess.created_at.asc())
        .limit(1)
    )
    row = res.first()
    return TreeContext(row[0], row[1]) if row else None


async def _default_tree_id(db: AsyncSession) -> Optional[uuid.UUID]:
    res = await db.execute(
        select(FamilyTree.id).order_by(FamilyTree.created_at.asc()).limit(1)
    )
    return res.scalar_one_or_none()


async def get_active_tree(
    x_tree_id: Optional[str] = Header(None, alias="X-Tree-ID"),
    tree_id_param: Optional[uuid.UUID] = Query(None, alias="tree_id"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TreeContext:
    """Arbre actif pour un endpoint authentifié. 403 si pas d'accès à l'arbre demandé."""
    tid = _parse_tid(x_tree_id, tree_id_param)

    if tid is not None:
        role = await _role_in_tree(db, current_user.id, tid)
        if role is not None:
            return TreeContext(tid, role)
        # Pas d'accès → refus strict (isolation des arbres).
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Accès refusé à cet arbre")

    # Aucun arbre précisé : premier arbre de l'utilisateur. S'il n'en a aucun,
    # il doit d'abord créer/rejoindre un arbre (onboarding).
    ctx = await _first_tree_for_user(db, current_user.id)
    if ctx is not None:
        return ctx
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Aucun arbre : faites votre onboarding d'abord")


async def get_active_tree_optional(
    x_tree_id: Optional[str] = Header(None, alias="X-Tree-ID"),
    tree_id_param: Optional[uuid.UUID] = Query(None, alias="tree_id"),
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
) -> Optional[TreeContext]:
    """Arbre actif pour un endpoint public (visiteur anonyme toléré)."""
    tid = _parse_tid(x_tree_id, tree_id_param)

    if current_user is not None:
        if tid is not None:
            role = await _role_in_tree(db, current_user.id, tid)
            if role is not None:
                return TreeContext(tid, role)
        else:
            ctx = await _first_tree_for_user(db, current_user.id)
            if ctx is not None:
                return ctx

    # Anonyme, ou pas d'accès : on retombe sur l'arbre demandé ou le défaut,
    # en lecture seule (visitor).
    if tid is not None:
        return TreeContext(tid, "visitor")
    default = await _default_tree_id(db)
    if default is not None:
        return TreeContext(default, "visitor")
    return None


def require_can_write(ctx: TreeContext) -> None:
    """Lève 403 si le rôle est lecture seule (visitor)."""
    if ctx.role == "visitor":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Lecture seule : vous êtes visiteur de cet arbre",
        )


def require_owner(ctx: TreeContext) -> None:
    if ctx.role != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Action réservée au propriétaire de l'arbre",
        )
