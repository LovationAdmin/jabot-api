import logging
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.schemas.auth import OTPRequest, OTPVerify, Token
from app.services import auth_service, sms_service

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/request-otp", status_code=200)
async def request_otp(body: OTPRequest, db: AsyncSession = Depends(get_db)):
    """
    Envoie un code OTP à 6 chiffres par SMS via Africa's Talking.
    Le code expire après 10 minutes.

    Repli "dev": si aucun fournisseur SMS n'est configuré
    (AFRICAS_TALKING_API_KEY absent), le code est renvoyé dans la réponse
    (champ `dev_code`) pour permettre les tests sans coût SMS.
    """
    sms_configured = bool(settings.AFRICAS_TALKING_API_KEY)

    code = auth_service.generate_otp()
    await auth_service.store_otp(body.phone, code)

    sent = await sms_service.send_otp_sms(body.phone, code)

    if not sent and sms_configured:
        # SMS configuré mais échec d'envoi → vraie erreur
        logger.error(f"Échec d'envoi OTP à {body.phone}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Impossible d'envoyer le SMS. Veuillez réessayer.",
        )

    response = {"message": "Code OTP envoyé avec succès", "phone": body.phone}
    if not sms_configured:
        # Pas de SMS configuré : on expose le code pour les tests
        logger.warning(f"[NO-SMS] Code OTP pour {body.phone}: {code}")
        response["dev_code"] = code
    return response


@router.post("/verify-otp", response_model=Token)
async def verify_otp(body: OTPVerify, db: AsyncSession = Depends(get_db)):
    """
    Vérifie le code OTP et retourne un token JWT valide 7 jours.
    """
    valid = await auth_service.verify_otp(body.phone, body.code)
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Code OTP invalide ou expiré",
        )

    user = await auth_service.get_or_create_user(db, body.phone)
    token = auth_service.create_access_token(str(user.id), user.phone)

    return Token(
        access_token=token,
        token_type="bearer",
        user_id=str(user.id),
        phone=user.phone,
    )
