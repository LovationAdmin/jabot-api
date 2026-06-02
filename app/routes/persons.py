import uuid
import logging
from typing import Optional, List
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.person import Person, CanvasPosition
from app.models.media import Media
from app.schemas.person import (
    PersonCreate, PersonUpdate, PersonResponse, PersonListResponse,
    SearchRequest, SearchMatch,
)
from app.middleware.auth import get_current_user, get_current_user_optional
from app.middleware.tree_context import (
    get_active_tree, get_active_tree_optional, TreeContext, require_can_write,
)
from app.services.ws_manager import manager as ws_manager
from app.models.user import User
from app.services.search_service import search_persons
from app.services.audit_service import write_audit
from app.services.tree_cache import invalidate_tree_cache

logger = logging.getLogger(__name__)
router = APIRouter()


# Champs réservés aux utilisateurs authentifiés (masqués pour les anonymes).
_SENSITIVE_PERSON_FIELDS = {
    "nicknames": None,
    "gender": None,
    "birth_date": None,
    "death_date": None,
    "city_of_origin": None,
    "media": None,
}


def _mask_person(p: PersonResponse) -> PersonResponse:
    """Ne conserve que le prénom + le nom (+ structure canvas) pour un anonyme."""
    return p.model_copy(update=_SENSITIVE_PERSON_FIELDS)


@router.get("", response_model=PersonListResponse)
async def list_persons(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    ctx: Optional[TreeContext] = Depends(get_active_tree_optional),
):
    """Liste les personnes de l'arbre actif. Un visiteur (anonyme ou invité) ne
    reçoit que le prénom + le nom ; les membres/propriétaires voient tout."""
    if ctx is None:
        return PersonListResponse(total=0, persons=[])
    full = ctx.role in ("owner", "member")

    count_result = await db.execute(
        select(func.count(Person.id)).where(
            Person.deleted_at.is_(None), Person.family_tree_id == ctx.tree_id
        )
    )
    total = count_result.scalar_one()

    result = await db.execute(
        select(Person)
        .options(selectinload(Person.canvas_position), selectinload(Person.media).selectinload(Media.uploaded_by).selectinload(User.person))
        .where(Person.deleted_at.is_(None), Person.family_tree_id == ctx.tree_id)
        .order_by(Person.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    persons = [PersonResponse.model_validate(p) for p in result.scalars().all()]
    if not full:
        persons = [_mask_person(p) for p in persons]
    return PersonListResponse(total=total, persons=persons)


@router.post("", response_model=PersonResponse, status_code=status.HTTP_201_CREATED)
async def create_person(
    body: PersonCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    ctx: TreeContext = Depends(get_active_tree),
):
    """Crée une nouvelle personne dans l'arbre actif (authentification requise)."""
    require_can_write(ctx)
    person = Person(
        id=uuid.uuid4(),
        family_tree_id=ctx.tree_id,
        first_name=body.first_name,
        last_name=body.last_name,
        nicknames=body.nicknames,
        gender=body.gender,
        birth_date=body.birth_date,
        death_date=body.death_date,
        city_of_origin=body.city_of_origin,
        created_by=current_user.id,
    )
    db.add(person)

    # Create default canvas position
    canvas_pos = CanvasPosition(person_id=person.id, x=0.0, y=0.0, generation=0)
    db.add(canvas_pos)

    await db.commit()
    result = await db.execute(
        select(Person)
        .options(selectinload(Person.canvas_position), selectinload(Person.media).selectinload(Media.uploaded_by).selectinload(User.person))
        .where(Person.id == person.id)
    )
    person = result.scalar_one()
    await write_audit(
        db,
        actor_user_id=current_user.id,
        action="create_person",
        entity_type="person",
        entity_id=str(person.id),
        details={"first_name": person.first_name, "last_name": person.last_name},
    )
    await invalidate_tree_cache(str(ctx.tree_id))
    await ws_manager.broadcast("person.created", {"person_id": str(person.id)}, str(current_user.id), tree_id=str(ctx.tree_id))
    return person


@router.get("/{person_id}", response_model=PersonResponse)
async def get_person(
    person_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    ctx: Optional[TreeContext] = Depends(get_active_tree_optional),
):
    """Récupère le détail d'une personne de l'arbre actif. Visiteur : prénom + nom."""
    if ctx is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Personne introuvable")
    result = await db.execute(
        select(Person)
        .options(selectinload(Person.canvas_position), selectinload(Person.media).selectinload(Media.uploaded_by).selectinload(User.person))
        .where(Person.id == person_id, Person.deleted_at.is_(None), Person.family_tree_id == ctx.tree_id)
    )
    person = result.scalar_one_or_none()
    if person is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Personne introuvable")
    resp = PersonResponse.model_validate(person)
    return resp if ctx.role in ("owner", "member") else _mask_person(resp)


@router.put("/{person_id}", response_model=PersonResponse)
async def update_person(
    person_id: uuid.UUID,
    body: PersonUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    ctx: TreeContext = Depends(get_active_tree),
):
    """Met à jour une personne de l'arbre actif (authentification requise)."""
    require_can_write(ctx)
    result = await db.execute(
        select(Person).where(
            Person.id == person_id, Person.deleted_at.is_(None),
            Person.family_tree_id == ctx.tree_id,
        )
    )
    person = result.scalar_one_or_none()
    if person is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Personne introuvable")

    update_data = body.model_dump(exclude_unset=True)
    changed_fields = list(update_data.keys())
    for field, value in update_data.items():
        setattr(person, field, value)
    person.updated_at = datetime.now(timezone.utc)

    await db.commit()
    result = await db.execute(
        select(Person)
        .options(selectinload(Person.canvas_position), selectinload(Person.media).selectinload(Media.uploaded_by).selectinload(User.person))
        .where(Person.id == person_id)
    )
    person = result.scalar_one()
    await write_audit(
        db,
        actor_user_id=current_user.id,
        action="update_person",
        entity_type="person",
        entity_id=str(person_id),
        details={
            "first_name": person.first_name,
            "last_name": person.last_name,
            "changed_fields": changed_fields,
        },
    )
    await invalidate_tree_cache(str(ctx.tree_id))
    await ws_manager.broadcast("person.updated", {"person_id": str(person_id)}, str(current_user.id), tree_id=str(ctx.tree_id))
    return person


@router.delete("/{person_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_person(
    person_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    ctx: TreeContext = Depends(get_active_tree),
):
    """Suppression douce d'une personne de l'arbre actif (authentification requise)."""
    require_can_write(ctx)
    result = await db.execute(
        select(Person).where(
            Person.id == person_id, Person.deleted_at.is_(None),
            Person.family_tree_id == ctx.tree_id,
        )
    )
    person = result.scalar_one_or_none()
    if person is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Personne introuvable")

    person.deleted_at = datetime.now(timezone.utc)
    details = {"first_name": person.first_name, "last_name": person.last_name}
    await db.commit()
    await write_audit(
        db,
        actor_user_id=current_user.id,
        action="delete_person",
        entity_type="person",
        entity_id=str(person_id),
        details=details,
    )
    await invalidate_tree_cache(str(ctx.tree_id))
    await ws_manager.broadcast("person.deleted", {"person_id": str(person_id)}, str(current_user.id), tree_id=str(ctx.tree_id))


@router.post("/search", response_model=List[SearchMatch])
async def search(
    body: SearchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """
    Recherche floue et phonétique de personnes pour l'onboarding.
    Combine pg_trgm trigram similarity + jellyfish phonétique + diminutifs ouest-africains.

    Confidentialité : la recherche s'effectue toujours sur les données complètes
    (le classement reste pertinent), mais un visiteur ANONYME ne reçoit en retour
    que le prénom + le nom de chaque résultat. Les autres champs (dates, ville,
    genre, photos) ne sont exposés qu'aux utilisateurs authentifiés.
    """
    if not body.name and not body.nickname and not body.parent_names and not body.sibling_names:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Au moins un critère de recherche est requis",
        )
    matches = await search_persons(db, body)

    if current_user is None:
        for m in matches:
            m.person = m.person.model_copy(update={
                "nicknames": None,
                "gender": None,
                "birth_date": None,
                "death_date": None,
                "city_of_origin": None,
                "media": None,
            })

    return matches
