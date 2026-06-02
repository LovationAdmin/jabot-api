import uuid
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.family_tree import FamilyTree, UserTreeAccess
from app.models.person import Person
from app.models.user import User
from app.middleware.auth import get_current_user
from app.middleware.tree_context import get_active_tree, TreeContext, require_owner
from app.schemas.tree_meta import (
    TreeListResponse, TreeAccessResponse, TreeCreateRequest, TreeRenameRequest,
    TreeMemberResponse, MemberRoleUpdate,
)
from app.services import tree_access_service
from app.services.tree_cache import invalidate_tree_cache

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("", response_model=TreeListResponse)
async def list_my_trees(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Liste tous les arbres auxquels l'utilisateur a accès, avec son rôle."""
    trees = await tree_access_service.list_user_trees(db, current_user.id)
    return TreeListResponse(trees=[TreeAccessResponse(**t) for t in trees])


@router.post("", response_model=TreeAccessResponse, status_code=status.HTTP_201_CREATED)
async def create_tree(
    body: TreeCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Crée un nouvel arbre vide ; l'appelant en devient propriétaire."""
    tree = await tree_access_service.create_tree(db, body.name or "Mon arbre", current_user.id)
    await db.commit()
    return TreeAccessResponse(tree_id=str(tree.id), tree_name=tree.name, role="owner")


@router.patch("/{tree_id}", response_model=TreeAccessResponse)
async def rename_tree(
    tree_id: uuid.UUID,
    body: TreeRenameRequest,
    db: AsyncSession = Depends(get_db),
    ctx: TreeContext = Depends(get_active_tree),
):
    """Renomme l'arbre (propriétaire uniquement)."""
    if ctx.tree_id != tree_id:
        raise HTTPException(status_code=400, detail="Arbre actif incohérent")
    require_owner(ctx)
    res = await db.execute(select(FamilyTree).where(FamilyTree.id == tree_id))
    tree = res.scalar_one_or_none()
    if tree is None:
        raise HTTPException(status_code=404, detail="Arbre introuvable")
    tree.name = body.name
    await db.commit()
    return TreeAccessResponse(tree_id=str(tree.id), tree_name=tree.name, role=ctx.role)


@router.delete("/{tree_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tree(
    tree_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    ctx: TreeContext = Depends(get_active_tree),
):
    """Supprime l'arbre et tout son contenu (propriétaire uniquement)."""
    if ctx.tree_id != tree_id:
        raise HTTPException(status_code=400, detail="Arbre actif incohérent")
    require_owner(ctx)
    res = await db.execute(select(FamilyTree).where(FamilyTree.id == tree_id))
    tree = res.scalar_one_or_none()
    if tree is None:
        raise HTTPException(status_code=404, detail="Arbre introuvable")
    # ON DELETE CASCADE sur persons / relationships / user_tree_access fait le reste.
    await db.delete(tree)
    await db.commit()
    await invalidate_tree_cache(str(tree_id))


@router.get("/{tree_id}/members", response_model=list[TreeMemberResponse])
async def list_members(
    tree_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    ctx: TreeContext = Depends(get_active_tree),
):
    """Liste les utilisateurs ayant accès à cet arbre (membres + visiteurs)."""
    if ctx.tree_id != tree_id:
        raise HTTPException(status_code=400, detail="Arbre actif incohérent")
    res = await db.execute(
        select(UserTreeAccess.user_id, UserTreeAccess.role, Person.first_name, Person.last_name)
        .join(User, User.id == UserTreeAccess.user_id)
        .outerjoin(Person, Person.id == User.person_id)
        .where(UserTreeAccess.family_tree_id == tree_id)
    )
    out = []
    for uid, role, fn, ln in res.all():
        name = " ".join(x for x in [fn, ln] if x) or None
        out.append(TreeMemberResponse(user_id=str(uid), role=role, person_name=name))
    return out


@router.patch("/{tree_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def change_member_role(
    tree_id: uuid.UUID,
    user_id: uuid.UUID,
    body: MemberRoleUpdate,
    db: AsyncSession = Depends(get_db),
    ctx: TreeContext = Depends(get_active_tree),
):
    """Change le rôle d'un membre (propriétaire uniquement)."""
    if ctx.tree_id != tree_id:
        raise HTTPException(status_code=400, detail="Arbre actif incohérent")
    require_owner(ctx)
    if body.role not in ("owner", "member", "visitor"):
        raise HTTPException(status_code=400, detail="Rôle invalide")
    res = await db.execute(
        select(UserTreeAccess).where(
            UserTreeAccess.user_id == user_id,
            UserTreeAccess.family_tree_id == tree_id,
        )
    )
    access = res.scalar_one_or_none()
    if access is None:
        raise HTTPException(status_code=404, detail="Membre introuvable")
    access.role = body.role
    await db.commit()


@router.delete("/{tree_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    tree_id: uuid.UUID,
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    ctx: TreeContext = Depends(get_active_tree),
):
    """Retire l'accès d'un utilisateur à l'arbre (propriétaire uniquement)."""
    if ctx.tree_id != tree_id:
        raise HTTPException(status_code=400, detail="Arbre actif incohérent")
    require_owner(ctx)
    await db.execute(
        delete(UserTreeAccess).where(
            UserTreeAccess.user_id == user_id,
            UserTreeAccess.family_tree_id == tree_id,
        )
    )
    await db.commit()
