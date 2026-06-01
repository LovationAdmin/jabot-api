"""
Système d'invitation par SMS.

INVITATION_ENABLED=False → le code est en place mais les invitations ne sont
pas actives. Les endpoints retournent 503 si la fonctionnalité est désactivée.
L'envoi SMS via Vonage est également conditionné par cette variable.
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
from app.middleware.auth import get_current_user
from app.security.crypto import phone_hash
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
    # En mode dev/SMS désactivé : code exposé pour tests
    dev_code: Optional[str] = None


class ValidateInvitationRequest(BaseModel):
    token: str
    code: str


class ValidateInvitationResponse(BaseModel):
    success: bool
    message: str


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _send_invitation_sms(phone: str, token: str, code: str) -> bool:
    """
    Envoie le SMS d'invitation via Vonage. Retourne True si envoyé.
    Désactivé si INVITATION_ENABLED=False ou SMS_DEV_MODE=True.
    """
    if not settings.INVITATION_ENABLED or settings.SMS_DEV_MODE:
        logger.info(f"[DEV] Invitation SMS → {phone} | token={token[:8]}… | code={code}")
        return False  # pas envoyé réellement

    try:
        import vonage  # type: ignore
        client = vonage.Client(key=settings.VONAGE_API_KEY, secret=settings.VONAGE_API_SECRET)
        sms = vonage.Sms(client)
        invite_url = f"{settings.FRONTEND_URL}/invite?token={token}"
        body = (
            f"Vous êtes invité à consulter l'arbre Jabot.\n"
            f"Votre code : {code}\n"
            f"Lien : {invite_url}"
        )
        resp = sms.send_message({
            "from": settings.VONAGE_BRAND_NAME,
            "to": phone,
            "text": body,
        })
        if resp["messages"][0]["status"] == "0":
            return True
        logger.warning(f"Vonage SMS failed: {resp}")
        return False
    except Exception as exc:
        logger.error(f"Vonage SMS error: {exc}")
        return False


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
):
    """Crée une invitation pour un numéro de téléphone (owner uniquement)."""
    _check_enabled()

    phone_clean = body.phone.strip()
    if not phone_clean.startswith("+"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Le numéro doit être au format international (+33612…)",
        )

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
        status="pending",
        expires_at=expires_at,
    )
    db.add(inv)

    sms_sent = await _send_invitation_sms(phone_clean, token, code)
    inv.sms_sent = sms_sent

    await db.commit()
    await db.refresh(inv)

    resp = CreateInvitationResponse(invitation_id=str(inv.id), token=token)
    if settings.SMS_DEV_MODE or not settings.INVITATION_ENABLED:
        resp.dev_code = code
    return resp


@router.post("/validate", response_model=ValidateInvitationResponse)
async def validate_invitation(
    body: ValidateInvitationRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """L'invité valide son token + code → reçoit un cookie de session visiteur."""
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
    await db.commit()

    _issue_visitor_cookie(response, body.token)

    return ValidateInvitationResponse(
        success=True,
        message="Invitation validée. Bienvenue sur Jabot !",
    )


@router.get("/check")
async def check_visitor_session(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Vérifie si le visiteur a une session valide (cookie)."""
    if not settings.INVITATION_ENABLED:
        # Feature off → tout le monde est considéré visiteur autorisé
        return {"valid": True, "reason": "open_access"}

    token = request.cookies.get(VISITOR_COOKIE_NAME)
    if not token:
        return {"valid": False, "reason": "no_cookie"}

    result = await db.execute(
        select(Invitation).where(Invitation.token == token, Invitation.status == "validated")
    )
    inv = result.scalar_one_or_none()
    if inv is None:
        return {"valid": False, "reason": "invalid_token"}

    return {"valid": True, "reason": "validated_invitation"}


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
