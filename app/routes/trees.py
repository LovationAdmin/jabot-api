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
    TreeMemberResponse, MemberRoleUpdate, TreeConvergeRequest, TreeConvergeResponse,
    PreConvergeScanRequest, PreConvergeScanResponse,
)
from app.services import tree_access_service
from app.services.tree_service import converge_trees
from app.services.cross_tree_match_service import scan_cross_tree_matches
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


@router.post("/{target_tree_id}/pre-converge-scan", response_model=PreConvergeScanResponse)
async def pre_converge_scan(
    target_tree_id: uuid.UUID,
    body: PreConvergeScanRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Scan pré-convergence : détecte les fiches de l'arbre source qui correspondent
    à des fiches de l'arbre cible, avant que l'utilisateur ne confirme la fusion.

    L'utilisateur doit être propriétaire de l'arbre source et avoir accès à la cible.
    """
    from app.services import tree_access_service as _tac
    src_role = await _tac.get_role(db, current_user.id, body.source_tree_id)
    if src_role != "owner":
        raise HTTPException(status_code=403, detail="Vous devez être propriétaire de l'arbre source.")
    tgt_role = await _tac.get_role(db, current_user.id, target_tree_id)
    if tgt_role is None:
        raise HTTPException(status_code=403, detail="Vous n'avez pas accès à l'arbre cible.")

    pairs, unmatched = await scan_cross_tree_matches(db, body.source_tree_id, target_tree_id)
    return PreConvergeScanResponse(proposed_pairs=pairs, unmatched_source_count=unmatched)


@router.post("/{target_tree_id}/converge", response_model=TreeConvergeResponse)
async def converge_into_tree(
    target_tree_id: uuid.UUID,
    body: TreeConvergeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Convergence : rapatrie l'arbre source de l'appelant dans l'arbre cible.

    Autorisation vérifiée dans le service : propriétaire de la source, accès à
    la cible, source non partagée. Opération atomique.

    additional_merge_pairs : paires confirmées par l'utilisateur via le scan
    pré-convergence (en plus de la fusion d'identité principale).
    """
    result = await converge_trees(
        db,
        user_id=current_user.id,
        source_tree_id=body.source_tree_id,
        target_tree_id=target_tree_id,
        source_person_id=body.source_person_id,
        target_person_id=body.target_person_id,
    )
    # Les deux arbres ont changé : on purge leurs caches.
    await invalidate_tree_cache(str(body.source_tree_id))
    await invalidate_tree_cache(str(target_tree_id))
    return TreeConvergeResponse(
        message=result["message"],
        source_tree_id=result["source_tree_id"],
        target_tree_id=result["target_tree_id"],
        persons_moved=result["persons_moved"],
        identity_merged=False,
        additional_merges=0,
    )


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
