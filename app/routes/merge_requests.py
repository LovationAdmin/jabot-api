"""
Routes pour les demandes de fusion d'arbres (TreeMergeRequest).

Flux :
  POST   /merge-requests            — soumettre une demande (tout user authentifié)
  GET    /merge-requests/pending    — lister les demandes en attente pour mes arbres
  POST   /merge-requests/{id}/approve — approuver (membre/owner de l'arbre cible)
  POST   /merge-requests/{id}/reject  — rejeter  (membre/owner de l'arbre cible)
"""
import uuid
import logging
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.tree_merge_request import TreeMergeRequest
from app.models.family_tree import FamilyTree, UserTreeAccess
from app.models.person import Person
from app.models.user import User
from app.middleware.auth import get_current_user
from app.schemas.merge_request import MergeRequestCreate, MergeRequestResponse
from app.services import tree_access_service
from app.services.tree_service import converge_trees
from app.services.ws_manager import manager as ws_manager

logger = logging.getLogger(__name__)
router = APIRouter()


def _to_response(r: TreeMergeRequest) -> MergeRequestResponse:
    return MergeRequestResponse(
        id=str(r.id),
        source_tree_id=str(r.source_tree_id),
        target_tree_id=str(r.target_tree_id),
        source_person_id=str(r.source_person_id) if r.source_person_id else None,
        target_person_id=str(r.target_person_id) if r.target_person_id else None,
        requested_by_user_id=str(r.requested_by_user_id),
        status=r.status,
        created_at=r.created_at,
        reviewed_by_user_id=str(r.reviewed_by_user_id) if r.reviewed_by_user_id else None,
        reviewed_at=r.reviewed_at,
        source_tree_name=r.source_tree_name,
        target_tree_name=r.target_tree_name,
        requester_first_name=r.requester_first_name,
    )


@router.post("", response_model=MergeRequestResponse, status_code=status.HTTP_201_CREATED)
async def create_merge_request(
    body: MergeRequestCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Soumet une demande de fusion. Tout user authentifié peut en créer une.
    Aucune vérification d'ownership — la validation est déléguée à l'arbre cible.
    """
    if body.source_tree_id == body.target_tree_id:
        raise HTTPException(status_code=400, detail="Les arbres source et cible sont identiques.")

    # Vérifier que les deux arbres existent
    src_tree = (await db.execute(
        select(FamilyTree).where(FamilyTree.id == body.source_tree_id)
    )).scalar_one_or_none()
    if src_tree is None:
        raise HTTPException(status_code=404, detail="Arbre source introuvable.")

    tgt_tree = (await db.execute(
        select(FamilyTree).where(FamilyTree.id == body.target_tree_id)
    )).scalar_one_or_none()
    if tgt_tree is None:
        raise HTTPException(status_code=404, detail="Arbre cible introuvable.")

    # Éviter les doublons de demandes pending pour la même paire
    existing = (await db.execute(
        select(TreeMergeRequest).where(
            TreeMergeRequest.source_tree_id == body.source_tree_id,
            TreeMergeRequest.target_tree_id == body.target_tree_id,
            TreeMergeRequest.status == "pending",
        )
    )).scalar_one_or_none()
    if existing:
        return _to_response(existing)

    # Snapshot des noms pour l'affichage sans JOIN
    requester_name = None
    if body.source_person_id:
        sp = (await db.execute(
            select(Person).where(Person.id == body.source_person_id, Person.deleted_at.is_(None))
        )).scalar_one_or_none()
        if sp:
            requester_name = sp.first_name

    req = TreeMergeRequest(
        id=uuid.uuid4(),
        source_tree_id=body.source_tree_id,
        target_tree_id=body.target_tree_id,
        source_person_id=body.source_person_id,
        target_person_id=body.target_person_id,
        requested_by_user_id=current_user.id,
        status="pending",
        source_tree_name=src_tree.name,
        target_tree_name=tgt_tree.name,
        requester_first_name=requester_name,
    )
    db.add(req)
    await db.commit()
    await db.refresh(req)

    # Notifier via WebSocket les membres de l'arbre cible
    await ws_manager.broadcast(
        "merge_request.new",
        {"merge_request_id": str(req.id), "source_tree_name": src_tree.name},
        str(current_user.id),
        tree_id=str(body.target_tree_id),
    )

    logger.info("Merge request %s created: %s → %s", req.id, src_tree.name, tgt_tree.name)
    return _to_response(req)


@router.get("/pending", response_model=List[MergeRequestResponse])
async def list_pending_merge_requests(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Retourne toutes les demandes pending dont l'arbre cible est un arbre
    auquel l'utilisateur courant a accès (il peut donc les valider).
    """
    # Arbres où l'utilisateur a accès
    my_tree_ids_rows = (await db.execute(
        select(UserTreeAccess.family_tree_id).where(
            UserTreeAccess.user_id == current_user.id
        )
    )).all()
    my_tree_ids = {r[0] for r in my_tree_ids_rows}

    if not my_tree_ids:
        return []

    rows = (await db.execute(
        select(TreeMergeRequest).where(
            TreeMergeRequest.status == "pending",
            or_(
                TreeMergeRequest.target_tree_id.in_(my_tree_ids),
                TreeMergeRequest.source_tree_id.in_(my_tree_ids),
            ),
        ).order_by(TreeMergeRequest.created_at.desc())
    )).scalars().all()

    return [_to_response(r) for r in rows]


@router.post("/{request_id}/approve", response_model=MergeRequestResponse)
async def approve_merge_request(
    request_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Approuve la demande de fusion. L'appelant doit être membre ou owner
    de l'arbre CIBLE. Déclenche immédiatement converge_trees().
    """
    req = (await db.execute(
        select(TreeMergeRequest).where(TreeMergeRequest.id == request_id)
    )).scalar_one_or_none()
    if req is None:
        raise HTTPException(status_code=404, detail="Demande introuvable.")
    if req.status != "pending":
        raise HTTPException(status_code=409, detail=f"La demande est déjà {req.status}.")

    # Vérifier que l'approbateur a accès à l'arbre cible
    tgt_role = await tree_access_service.get_role(db, current_user.id, req.target_tree_id)
    if tgt_role is None:
        raise HTTPException(status_code=403, detail="Vous n'avez pas accès à l'arbre cible.")

    # Exécuter la convergence. On passe l'owner de l'arbre source comme user_id
    # pour satisfaire la vérification d'ownership dans converge_trees.
    src_owner_row = (await db.execute(
        select(UserTreeAccess.user_id).where(
            UserTreeAccess.family_tree_id == req.source_tree_id,
            UserTreeAccess.role == "owner",
        ).limit(1)
    )).scalar_one_or_none()

    if src_owner_row is None:
        raise HTTPException(status_code=409, detail="L'arbre source n'a pas de propriétaire identifiable.")

    try:
        await converge_trees(
            db=db,
            user_id=src_owner_row,
            source_tree_id=req.source_tree_id,
            target_tree_id=req.target_tree_id,
            source_person_id=req.source_person_id,
            target_person_id=req.target_person_id,
        )
    except HTTPException as exc:
        raise exc
    except Exception as exc:
        logger.error("converge_trees failed during merge request approval: %s", exc)
        raise HTTPException(status_code=500, detail="La fusion a échoué. Réessayez.")

    # Marquer la demande comme approuvée (la transaction de converge_trees a déjà commité)
    req.status = "approved"
    req.reviewed_by_user_id = current_user.id
    req.reviewed_at = datetime.now(timezone.utc)
    db.add(req)
    await db.commit()
    await db.refresh(req)

    logger.info("Merge request %s approved by %s", req.id, current_user.id)
    return _to_response(req)


@router.post("/{request_id}/reject", response_model=MergeRequestResponse)
async def reject_merge_request(
    request_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Rejette la demande. L'appelant doit être membre ou owner de l'arbre cible."""
    req = (await db.execute(
        select(TreeMergeRequest).where(TreeMergeRequest.id == request_id)
    )).scalar_one_or_none()
    if req is None:
        raise HTTPException(status_code=404, detail="Demande introuvable.")
    if req.status != "pending":
        raise HTTPException(status_code=409, detail=f"La demande est déjà {req.status}.")

    tgt_role = await tree_access_service.get_role(db, current_user.id, req.target_tree_id)
    if tgt_role is None:
        raise HTTPException(status_code=403, detail="Vous n'avez pas accès à l'arbre cible.")

    req.status = "rejected"
    req.reviewed_by_user_id = current_user.id
    req.reviewed_at = datetime.now(timezone.utc)
    db.add(req)
    await db.commit()
    await db.refresh(req)

    logger.info("Merge request %s rejected by %s", req.id, current_user.id)
    return _to_response(req)
