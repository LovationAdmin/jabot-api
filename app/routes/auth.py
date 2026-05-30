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
    Envoie un code OTP a 6 chiffres par SMS (Vonage, puis Africa's Talking).
    Le code expire apres 10 minutes.

    Repli "dev_code": le code est renvoye dans la reponse (champ `dev_code`)
    lorsque l'envoi SMS n'est pas possible, c'est-a-dire :
      - aucun fournisseur SMS n'est configure, OU
      - l'envoi a echoue (ex: compte Vonage encore en trial / paiement pending).
    Cela permet de tester tout le flux OTP sans bloquer sur le SMS.
    """
    sms_configured = sms_service.is_configured()

    code = auth_service.generate_otp()
    await auth_service.store_otp(body.phone, code)

    sent = await sms_service.send_otp_sms(body.phone, code)

    response = {"phone": body.phone}

    if sent and sms_configured:
        response["message"] = "Code OTP envoye avec succes"
    else:
        # Soit pas de fournisseur, soit echec d'envoi : on expose le code
        # pour ne pas bloquer l'utilisateur pendant la phase de mise en place.
        if sms_configured and not sent:
            logger.warning(f"SMS non envoye a {body.phone}, repli dev_code actif")
        else:
            logger.warning(f"[NO-SMS] Code OTP pour {body.phone}: {code}")
        response["message"] = "SMS indisponible, utilisez le code de test"
        response["dev_code"] = code

    return response


@router.post("/verify-otp", response_model=Token)
async def verify_otp(body: OTPVerify, db: AsyncSession = Depends(get_db)):
    """
    Verifie le code OTP et retourne un token JWT valide 7 jours.
    """
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
    )
