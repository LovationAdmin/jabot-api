"""
Système d'invitation par SMS.

INVITATION_ENABLED=False → le code est en place mais les invitations ne sont
pas actives. Les endpoints retournent 503 si la fonctionnalité est désactivée.

L'envoi passe par sms_service (Termii) et consomme le quota par numéro
(sms_quota_service : 2 SMS / numéro / 24 h). Si le SMS ne part pas, le lien
+ code sont retournés à l'inviteur pour un partage manuel (WhatsApp…).
"""
import uuid
import secrets
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status, Response, Request, Cookie
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models.invitation import Invitation
from app.models.user import User
from app.middleware.auth import get_current_user, get_current_user_optional
from app.middleware.tree_context import get_active_tree, TreeContext, require_can_write
from app.security.crypto import phone_hash
from app.services import sms_quota_service, sms_service, tree_access_service
from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()

# ── Feature flag ──────────────────────────────────────────────────────────────
INVITATION_TTL_HOURS: int = 72
VISITOR_COOKIE_NAME: str = "jabot_visitor"
VISITOR_COOKIE_TTL_DAYS: int = 30


def _check_enabled():
    if not settings.INVITATION_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Le système d'invitation n'est pas encore activé.",
        )


# ── Schemas ───────────────────────────────────────────────────────────────────

class CreateInvitationRequest(BaseModel):
    phone: str  # numéro au format E.164 (+33612...)


class CreateInvitationResponse(BaseModel):
    invitation_id: str
    token: str  # à intégrer dans le lien SMS
    sms_sent: bool = False
    # Code exposé quand le SMS n'est pas parti (mode dev, feature désactivée
    # ou échec d'envoi) : l'inviteur le partage lui-même avec le lien.
    dev_code: Optional[str] = None


class ValidateInvitationRequest(BaseModel):
    token: str
    code: str


class ValidateInvitationResponse(BaseModel):
    success: bool
    message: str
    tree_id: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _send_invitation_sms(phone: str, token: str, code: str) -> bool:
    """
    Envoie le SMS d'invitation via Termii. Retourne True si envoyé.
    Pas d'envoi réel si INVITATION_ENABLED=False ou SMS_DEV_MODE=True :
    le code est alors exposé dans la réponse.
    """
    if not settings.INVITATION_ENABLED or settings.SMS_DEV_MODE:
        logger.info(f"[DEV] Invitation SMS → {phone} | token={token[:8]}… | code={code}")
        return False  # pas envoyé réellement

    invite_url = f"{settings.FRONTEND_URL}/invite?token={token}"
    body = (
        f"Lovation - Vous êtes invité(e) à découvrir l'arbre familial sur Jabotai.com. "
        f"Code : {code}. Lien : {invite_url}"
    )
    return await sms_service.send_sms(phone, body)


def _issue_visitor_cookie(response: Response, token: str) -> None:
    """Pose le cookie de session visiteur validé."""
    response.set_cookie(
        key=VISITOR_COOKIE_NAME,
        value=token,
        max_age=VISITOR_COOKIE_TTL_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=settings.ENVIRONMENT != "development",
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/", response_model=CreateInvitationResponse, status_code=status.HTTP_201_CREATED)
async def create_invitation(
    body: CreateInvitationRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    ctx: TreeContext = Depends(get_active_tree),
):
    """Crée une invitation pour un numéro (tout membre/propriétaire de l'arbre)."""
    _check_enabled()
    require_can_write(ctx)  # visiteurs ne peuvent pas inviter

    phone_clean = body.phone.strip()
    if not phone_clean.startswith("+"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Le numéro doit être au format international (+33612…)",
        )

    # Quota Termii par numéro (mêmes règles que l'OTP : 2 SMS / numéro / 24 h).
    # Vérifié avant toute écriture ; rendu plus bas si l'envoi échoue.
    await sms_quota_service.enforce(phone_clean)

    p_hash = phone_hash(phone_clean)

    # Revoke existing pending invitations for the same number
    existing = await db.execute(
        select(Invitation).where(
            Invitation.invited_phone_hash == p_hash,
            Invitation.status == "pending",
        )
    )
    for inv in existing.scalars().all():
        inv.status = "revoked"

    token = secrets.token_urlsafe(32)
    code = f"{secrets.randbelow(1_000_000):06d}"
    expires_at = datetime.now(timezone.utc) + timedelta(hours=INVITATION_TTL_HOURS)

    inv = Invitation(
        id=uuid.uuid4(),
        token=token,
        validation_code=code,
        invited_phone=phone_clean,
        invited_phone_hash=p_hash,
        inviter_user_id=current_user.id,
        family_tree_id=ctx.tree_id,
        status="pending",
        expires_at=expires_at,
    )
    db.add(inv)

    sms_sent = await _send_invitation_sms(phone_clean, token, code)
    inv.sms_sent = sms_sent
    if not sms_sent and not settings.SMS_DEV_MODE:
        # Envoi réel tenté mais échoué : on rend le créneau de quota.
        await sms_quota_service.release_send(phone_clean)

    await db.commit()
    await db.refresh(inv)

    resp = CreateInvitationResponse(invitation_id=str(inv.id), token=token, sms_sent=sms_sent)
    if not sms_sent:
        # SMS non parti (dev, feature off ou échec) : l'inviteur partage le
        # lien + code lui-même (WhatsApp…), l'invitation reste utilisable.
        resp.dev_code = code
    return resp


@router.post("/validate", response_model=ValidateInvitationResponse)
async def validate_invitation(
    body: ValidateInvitationRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """L'invité valide son token + code.

    - Si l'utilisateur est authentifié (Bearer token) → octroie rôle visiteur
      sur l'arbre associé à l'invitation + pose le cookie de session.
    - Sinon → cookie visiteur seul (accès anonyme limité).
    """
    _check_enabled()

    result = await db.execute(
        select(Invitation).where(Invitation.token == body.token)
    )
    inv = result.scalar_one_or_none()

    if inv is None or inv.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invitation introuvable ou déjà utilisée.",
        )

    if datetime.now(timezone.utc) > inv.expires_at:
        inv.status = "expired"
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Cette invitation a expiré.",
        )

    if inv.validation_code != body.code.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Code de validation incorrect.",
        )

    inv.status = "validated"
    inv.validated_at = datetime.now(timezone.utc)

    # Grant authenticated user visitor access to the invitation's tree.
    if current_user is not None and inv.family_tree_id is not None:
        await tree_access_service.grant_access(db, current_user.id, inv.family_tree_id, "visitor")

    await db.commit()

    _issue_visitor_cookie(response, body.token)

    return ValidateInvitationResponse(
        success=True,
        message="Invitation validée. Bienvenue sur Jabot !",
        tree_id=str(inv.family_tree_id) if inv.family_tree_id else None,
    )


@router.get("/check")
async def check_visitor_session(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Vérifie si le visiteur a une session valide (cookie) et retourne l'arbre associé."""
    token = request.cookies.get(VISITOR_COOKIE_NAME)
    if not token:
        return {"valid": False, "reason": "no_cookie"}

    result = await db.execute(
        select(Invitation).where(Invitation.token == token, Invitation.status == "validated")
    )
    inv = result.scalar_one_or_none()
    if inv is None:
        return {"valid": False, "reason": "invalid_token"}

    return {
        "valid": True,
        "reason": "validated_invitation",
        "tree_id": str(inv.family_tree_id) if inv.family_tree_id else None,
    }


@router.get("/list")
async def list_invitations(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Liste les invitations émises par l'utilisateur courant."""
    _check_enabled()

    result = await db.execute(
        select(Invitation)
        .where(Invitation.inviter_user_id == current_user.id)
        .order_by(Invitation.created_at.desc())
    )
    invitations = result.scalars().all()
    return [
        {
            "id": str(inv.id),
            "status": inv.status,
            "sms_sent": inv.sms_sent,
            "expires_at": inv.expires_at.isoformat(),
            "created_at": inv.created_at.isoformat(),
            "validated_at": inv.validated_at.isoformat() if inv.validated_at else None,
        }
        for inv in invitations
    ]
