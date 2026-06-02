import logging
import uuid
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db
from app.schemas.auth import OTPRequest, OTPVerify, Token, MeResponse, LinkPersonRequest, TreeAccessItem
from app.schemas.person import PersonCreate, PersonResponse
from app.services import auth_service, sms_service, tree_access_service
from app.middleware.auth import get_current_user
from app.models.user import User
from app.models.person import Person, CanvasPosition

logger = logging.getLogger(__name__)
router = APIRouter()


async def _load_person(db: AsyncSession, person_id: uuid.UUID) -> Person:
    """Charge une fiche avec ses relations (canvas_position, media) pour serialisation."""
    result = await db.execute(
        select(Person)
        .options(selectinload(Person.canvas_position), selectinload(Person.media))
        .where(Person.id == person_id)
    )
    return result.scalar_one()


async def _tree_state(db: AsyncSession, user_id: uuid.UUID) -> tuple[list[TreeAccessItem], Optional[str]]:
    """Retourne (liste des accès arbres, id de l'arbre actif par défaut)."""
    trees = await tree_access_service.list_user_trees(db, user_id)
    items = [TreeAccessItem(**t) for t in trees]
    active = items[0].tree_id if items else None
    return items, active


@router.post("/request-otp", status_code=200)
async def request_otp(body: OTPRequest, db: AsyncSession = Depends(get_db)):
    code = auth_service.generate_otp()
    await auth_service.store_otp(body.phone, code)

    sent = await sms_service.send_otp_sms(body.phone, code)

    if settings.SMS_DEV_MODE:
        logger.info(f"[SMS_DEV_MODE] Code OTP pour {body.phone}: {code}")
        return {
            "phone": body.phone,
            "message": "Mode dev : utilisez le code affiché",
            "dev_code": code,
        }

    if not sent:
        logger.error(f"Echec d'envoi du SMS OTP a {body.phone}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Impossible d'envoyer le SMS pour le moment. Réessayez dans un instant.",
        )

    return {"phone": body.phone, "message": "Code OTP envoyé avec succès"}


@router.post("/verify-otp", response_model=Token)
async def verify_otp(body: OTPVerify, db: AsyncSession = Depends(get_db)):
    valid = await auth_service.verify_otp(body.phone, body.code)
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Code OTP invalide ou expire",
        )

    user = await auth_service.get_or_create_user(db, body.phone)
    token = auth_service.create_access_token(str(user.id), user.phone)
    tree_accesses, active_tree_id = await _tree_state(db, user.id)

    return Token(
        access_token=token,
        token_type="bearer",
        user_id=str(user.id),
        phone=user.phone,
        person_id=str(user.person_id) if user.person_id else None,
        onboarded=user.person_id is not None,
        tree_accesses=tree_accesses,
        active_tree_id=active_tree_id,
    )


@router.get("/me", response_model=MeResponse)
async def me(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Session glissante : on réémet un token frais à chaque appel /me.
    fresh_token = auth_service.create_access_token(str(current_user.id), current_user.phone)
    tree_accesses, active_tree_id = await _tree_state(db, current_user.id)
    return MeResponse(
        user_id=str(current_user.id),
        phone=current_user.phone,
        person_id=str(current_user.person_id) if current_user.person_id else None,
        onboarded=current_user.person_id is not None,
        access_token=fresh_token,
        tree_accesses=tree_accesses,
        active_tree_id=active_tree_id,
    )


@router.post("/link-person", response_model=MeResponse)
async def link_person(
    body: LinkPersonRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.person_id is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Vous etes deja rattache a une fiche")

    result = await db.execute(
        select(Person).where(Person.id == body.person_id, Person.deleted_at.is_(None))
    )
    person = result.scalar_one_or_none()
    if person is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fiche introuvable")

    current_user.person_id = person.id
    # Rattacher l'utilisateur à l'arbre de la fiche → rôle membre.
    await tree_access_service.grant_access(db, current_user.id, person.family_tree_id, "member")
    await db.commit()
    await db.refresh(current_user)

    tree_accesses, active_tree_id = await _tree_state(db, current_user.id)
    return MeResponse(
        user_id=str(current_user.id),
        phone=current_user.phone,
        person_id=str(current_user.person_id),
        onboarded=True,
        tree_accesses=tree_accesses,
        active_tree_id=str(person.family_tree_id),
    )


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def delete_me(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Supprime le compte de l'utilisateur connecté ; la/les fiches restent."""
    from sqlalchemy import update
    from app.models.person import Person as PersonModel

    await db.execute(
        update(PersonModel)
        .where(PersonModel.created_by == current_user.id)
        .values(created_by=None)
    )

    await db.delete(current_user)
    await db.commit()


@router.post("/onboard", response_model=PersonResponse, status_code=status.HTTP_201_CREATED)
async def onboard(
    body: PersonCreate,
    tree_id: Optional[uuid.UUID] = Query(None, description="Arbre cible ; absent ⇒ arbre par défaut / nouvel arbre"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Crée la fiche de l'utilisateur et la rattache à un arbre.

    Phase 1 (comportement préservé) :
      - si `tree_id` fourni : crée la fiche dans cet arbre (rôle membre)
      - sinon : rattache à l'arbre par défaut s'il existe, sinon crée un
        nouvel arbre dont l'utilisateur devient propriétaire.
    """
    if current_user.person_id is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Vous etes deja rattache a une fiche")

    # Déterminer l'arbre cible.
    #   - tree_id fourni  → rejoint cet arbre existant en tant que membre
    #     (cas « je fais partie de cette famille », choisi via le matching)
    #   - aucun tree_id   → démarre un NOUVEL arbre dont il est propriétaire
    #     (cas « aucune correspondance, ma famille commence ici »)
    target_tree_id = tree_id
    role = "member"
    if target_tree_id is None:
        tree_name = f"Famille {body.last_name}" if body.last_name else "Mon arbre"
        new_tree = await tree_access_service.create_tree(db, tree_name, current_user.id)
        target_tree_id = new_tree.id
        role = "owner"

    person_id = uuid.uuid4()
    person = Person(
        id=person_id,
        family_tree_id=target_tree_id,
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
    db.add(CanvasPosition(person_id=person_id, x=0.0, y=0.0, generation=0))
    current_user.person_id = person_id
    if role != "owner":
        await tree_access_service.grant_access(db, current_user.id, target_tree_id, role)
    await db.commit()

    return await _load_person(db, person_id)
