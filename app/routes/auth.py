import logging
import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db
from app.schemas.auth import OTPRequest, OTPVerify, Token, MeResponse, LinkPersonRequest
from app.schemas.person import PersonCreate, PersonResponse
from app.services import auth_service, sms_service
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


@router.post("/request-otp", status_code=200)
async def request_otp(body: OTPRequest, db: AsyncSession = Depends(get_db)):
    sms_configured = sms_service.is_configured()

    code = auth_service.generate_otp()
    await auth_service.store_otp(body.phone, code)

    sent = await sms_service.send_otp_sms(body.phone, code)

    response = {"phone": body.phone}

    if sent and sms_configured:
        response["message"] = "Code OTP envoye avec succes"
    else:
        if sms_configured and not sent:
            logger.warning(f"SMS non envoye a {body.phone}, repli dev_code actif")
        else:
            logger.warning(f"[NO-SMS] Code OTP pour {body.phone}: {code}")
        response["message"] = "SMS indisponible, utilisez le code de test"
        response["dev_code"] = code

    return response


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

    return Token(
        access_token=token,
        token_type="bearer",
        user_id=str(user.id),
        phone=user.phone,
        person_id=str(user.person_id) if user.person_id else None,
        onboarded=user.person_id is not None,
    )


@router.get("/me", response_model=MeResponse)
async def me(current_user: User = Depends(get_current_user)):
    return MeResponse(
        user_id=str(current_user.id),
        phone=current_user.phone,
        person_id=str(current_user.person_id) if current_user.person_id else None,
        onboarded=current_user.person_id is not None,
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
    await db.commit()
    await db.refresh(current_user)

    return MeResponse(
        user_id=str(current_user.id),
        phone=current_user.phone,
        person_id=str(current_user.person_id),
        onboarded=True,
    )


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def delete_me(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Supprime le compte de l'utilisateur connecté.

    La fiche Person liée est conservée dans l'arbre ; seul le lien est rompu
    en mettant persons.created_by = NULL pour toutes les fiches créées par
    cet utilisateur. L'enregistrement User est ensuite supprimé.
    """
    from sqlalchemy import update
    from app.models.person import Person as PersonModel

    # Nullifier created_by sur les fiches créées par cet utilisateur
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
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.person_id is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Vous etes deja rattache a une fiche")

    person_id = uuid.uuid4()
    person = Person(
        id=person_id,
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
    await db.commit()

    # Rechargement avec les relations pour eviter MissingGreenlet a la serialisation
    return await _load_person(db, person_id)
