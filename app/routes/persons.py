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
from app.schemas.person import (
    PersonCreate, PersonUpdate, PersonResponse, PersonListResponse,
    SearchRequest, SearchMatch,
)
from app.middleware.auth import get_current_user, get_current_user_optional
from app.models.user import User
from app.services.search_service import search_persons
from app.services.audit_service import write_audit

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("", response_model=PersonListResponse)
async def list_persons(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Liste toutes les personnes (public, sans authentification)."""
    count_result = await db.execute(
        select(func.count(Person.id)).where(Person.deleted_at.is_(None))
    )
    total = count_result.scalar_one()

    result = await db.execute(
        select(Person)
        .options(selectinload(Person.canvas_position), selectinload(Person.media))
        .where(Person.deleted_at.is_(None))
        .order_by(Person.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    persons = result.scalars().all()
    return PersonListResponse(total=total, persons=list(persons))


@router.post("", response_model=PersonResponse, status_code=status.HTTP_201_CREATED)
async def create_person(
    body: PersonCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Crée une nouvelle personne dans l'arbre (authentification requise)."""
    person = Person(
        id=uuid.uuid4(),
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
        .options(selectinload(Person.canvas_position), selectinload(Person.media))
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
    return person


@router.get("/{person_id}", response_model=PersonResponse)
async def get_person(
    person_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Récupère le détail d'une personne."""
    result = await db.execute(
        select(Person)
        .options(selectinload(Person.canvas_position), selectinload(Person.media))
        .where(Person.id == person_id, Person.deleted_at.is_(None))
    )
    person = result.scalar_one_or_none()
    if person is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Personne introuvable")
    return person


@router.put("/{person_id}", response_model=PersonResponse)
async def update_person(
    person_id: uuid.UUID,
    body: PersonUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Met à jour une personne (authentification requise)."""
    result = await db.execute(
        select(Person).where(Person.id == person_id, Person.deleted_at.is_(None))
    )
    person = result.scalar_one_or_none()
    if person is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Personne introuvable")

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(person, field, value)
    person.updated_at = datetime.now(timezone.utc)

    await db.commit()
    result = await db.execute(
        select(Person)
        .options(selectinload(Person.canvas_position), selectinload(Person.media))
        .where(Person.id == person_id)
    )
    person = result.scalar_one()
    await write_audit(
        db,
        actor_user_id=current_user.id,
        action="update_person",
        entity_type="person",
        entity_id=str(person_id),
        details={"first_name": person.first_name, "last_name": person.last_name},
    )
    return person


@router.delete("/{person_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_person(
    person_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Suppression douce d'une personne (authentification requise)."""
    result = await db.execute(
        select(Person).where(Person.id == person_id, Person.deleted_at.is_(None))
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


@router.post("/search", response_model=List[SearchMatch])
async def search(
    body: SearchRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Recherche floue et phonétique de personnes pour l'onboarding.
    Combine pg_trgm trigram similarity + jellyfish phonétique + diminutifs ouest-africains.
    """
    if not body.name and not body.nickname and not body.parent_names and not body.sibling_names:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Au moins un critère de recherche est requis",
        )
    matches = await search_persons(db, body)
    return matches
