import logging
import uuid
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db
from app.schemas.auth import (
    OTPRequest, OTPVerify, Token, MeResponse, LinkPersonRequest, TreeAccessItem,
    OnboardSearchRequest, OnboardSearchResponse, OnboardMatch, MatchRelative,
    PhoneChangeRequest, PhoneChangeConfirm,
)
from app.schemas.person import PersonCreate, PersonResponse, SearchRequest
from app.services import auth_service, sms_service, tree_access_service
from app.services.search_service import search_persons
from app.middleware.auth import get_current_user
from app.models.user import User
from app.models.person import Person, CanvasPosition
from app.models.relationship import Relationship
from app.models.family_tree import FamilyTree

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

    # Vérifie que la fiche rattachée n'a pas été supprimée entre temps.
    if current_user.person_id is not None:
        check = await db.execute(
            select(Person).where(
                Person.id == current_user.person_id,
                Person.deleted_at.is_(None),
            )
        )
        if check.scalar_one_or_none() is None:
            current_user.person_id = None
            await db.commit()

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


async def _immediate_family(db: AsyncSession, person_id: uuid.UUID, tree_id: uuid.UUID) -> tuple[list[MatchRelative], list[MatchRelative]]:
    """Parents + fratrie directe d'une personne, dans son arbre."""
    rels = (await db.execute(
        select(Relationship).where(
            Relationship.family_tree_id == tree_id,
            (Relationship.person_a_id == person_id) | (Relationship.person_b_id == person_id),
        )
    )).scalars().all()

    parent_ids: list[uuid.UUID] = []
    sibling_ids: list[uuid.UUID] = []
    for r in rels:
        if r.type == "parent" and r.person_b_id == person_id:
            parent_ids.append(r.person_a_id)          # A est parent de la personne
        elif r.type in ("sibling", "half_sibling", "step_sibling"):
            other = r.person_a_id if r.person_b_id == person_id else r.person_b_id
            sibling_ids.append(other)

    out_parents, out_siblings = [], []
    all_ids = list({*parent_ids, *sibling_ids})
    if all_ids:
        persons = (await db.execute(
            select(Person).where(Person.id.in_(all_ids), Person.deleted_at.is_(None))
        )).scalars().all()
        by_id = {p.id: p for p in persons}
        for pid in parent_ids:
            p = by_id.get(pid)
            if p:
                out_parents.append(MatchRelative(first_name=p.first_name, last_name=p.last_name))
        for sid in sibling_ids:
            p = by_id.get(sid)
            if p:
                out_siblings.append(MatchRelative(first_name=p.first_name, last_name=p.last_name))
    return out_parents, out_siblings


@router.post("/onboard-search", response_model=OnboardSearchResponse)
async def onboard_search(
    body: OnboardSearchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Cherche, à travers TOUS les arbres, des fiches correspondant à l'identité
    saisie pendant l'onboarding. Regroupe par arbre et joint le contexte familial
    immédiat (parents + fratrie) pour que l'utilisateur reconnaisse sa famille.

    Retourne au plus une correspondance (la meilleure) par arbre.
    """
    if not body.name and not body.nickname and not body.parent_names and not body.sibling_names:
        return OnboardSearchResponse(matches=[])

    matches = await search_persons(db, SearchRequest(
        name=body.name, nickname=body.nickname,
        parent_names=body.parent_names, sibling_names=body.sibling_names,
        city_of_origin=body.city_of_origin,
    ))

    # Garder la meilleure correspondance par arbre.
    best_per_tree: dict[str, OnboardMatch] = {}
    # Charger les arbres des personnes correspondantes en une fois.
    pid_list = [uuid.UUID(m.person.id) if isinstance(m.person.id, str) else m.person.id for m in matches]
    tree_of: dict[str, uuid.UUID] = {}
    if pid_list:
        rows = (await db.execute(
            select(Person.id, Person.family_tree_id).where(Person.id.in_(pid_list))
        )).all()
        tree_of = {str(pid): tid for pid, tid in rows}

    tree_names: dict[uuid.UUID, str] = {}
    for m in matches:
        pid = str(m.person.id)
        tid = tree_of.get(pid)
        if tid is None:
            continue
        if str(tid) in best_per_tree and best_per_tree[str(tid)].confidence >= m.confidence:
            continue
        if tid not in tree_names:
            tn = (await db.execute(select(FamilyTree.name).where(FamilyTree.id == tid))).scalar_one_or_none()
            tree_names[tid] = tn or "Arbre"
        parents, siblings = await _immediate_family(db, uuid.UUID(pid), tid)
        best_per_tree[str(tid)] = OnboardMatch(
            tree_id=str(tid),
            tree_name=tree_names[tid],
            person_id=pid,
            first_name=m.person.first_name,
            last_name=m.person.last_name,
            birth_date=m.person.birth_date.isoformat() if getattr(m.person, "birth_date", None) else None,
            confidence=round(m.confidence, 2),
            parents=parents,
            siblings=siblings,
        )

    ordered = sorted(best_per_tree.values(), key=lambda x: x.confidence, reverse=True)
    return OnboardSearchResponse(matches=ordered)


@router.post("/request-phone-change", status_code=200)
async def request_phone_change(
    body: PhoneChangeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Envoie un OTP au nouveau numéro pour valider le changement."""
    from app.security.crypto import phone_hash as ph_hash
    existing = (await db.execute(
        select(User).where(User.phone_hash == ph_hash(body.new_phone))
    )).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Ce numéro est déjà associé à un autre compte.")

    code = auth_service.generate_otp()
    await auth_service.store_otp(body.new_phone, code)
    sent = await sms_service.send_otp_sms(body.new_phone, code)

    if settings.SMS_DEV_MODE:
        logger.info(f"[SMS_DEV_MODE] Code OTP changement tél pour {body.new_phone}: {code}")
        return {"message": "Mode dev : utilisez le code affiché", "dev_code": code}

    if not sent:
        raise HTTPException(status_code=503, detail="Impossible d'envoyer le SMS. Réessayez.")

    return {"message": "Code OTP envoyé au nouveau numéro"}


@router.post("/confirm-phone-change", status_code=200)
async def confirm_phone_change(
    body: PhoneChangeConfirm,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Valide l'OTP et met à jour le numéro de téléphone du compte."""
    from app.security.crypto import phone_hash as ph_hash
    valid = await auth_service.verify_otp(body.new_phone, body.code)
    if not valid:
        raise HTTPException(status_code=400, detail="Code OTP invalide ou expiré.")

    existing = (await db.execute(
        select(User).where(User.phone_hash == ph_hash(body.new_phone))
    )).scalar_one_or_none()
    if existing is not None and existing.id != current_user.id:
        raise HTTPException(status_code=409, detail="Ce numéro est déjà associé à un autre compte.")

    current_user.phone = body.new_phone
    current_user.phone_hash = ph_hash(body.new_phone)
    await db.commit()
    return {"message": "Numéro mis à jour avec succès."}


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
