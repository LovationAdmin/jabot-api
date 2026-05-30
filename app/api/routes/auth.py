from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, field_validator
import random
import logging

from app.config import settings
from app.services import sms_service
from app.services.otp_service import store_otp, verify_otp as verify_otp_code
from app.services.auth_service import create_access_token
from app.services import user_service
from app.db.session import get_redis

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class RequestOtpBody(BaseModel):
    phone: str

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        cleaned = v.replace(" ", "").replace("-", "")
        if not cleaned.startswith("+"):
            raise ValueError("Le numéro doit être au format international (+...)")
        if len(cleaned) < 8 or len(cleaned) > 16:
            raise ValueError("Numéro de téléphone invalide")
        return cleaned


class VerifyOtpBody(BaseModel):
    phone: str
    code: str


class OtpResponse(BaseModel):
    message: str
    phone: str
    dev_code: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    phone: str


@router.post("/request-otp", response_model=OtpResponse)
async def request_otp(body: RequestOtpBody):
    code = f"{random.randint(0, 999999):06d}"
    phone = body.phone

    await store_otp(phone, code)

    sent = await sms_service.send_otp_sms(phone, code)

    # On expose le code de test quand :
    #   - on est en développement, OU
    #   - aucun fournisseur SMS réel n'est configuré, OU
    #   - l'envoi a échoué (ex: compte Vonage encore en trial / paiement pending).
    # Cela permet de tester tout le flux OTP sans dépendre du SMS.
    dev_code = None
    if settings.ENVIRONMENT == "development" or not sms_service.is_configured() or not sent:
        dev_code = code

    if not sent and sms_service.is_configured():
        logger.warning(f"SMS non envoyé à {phone}, code exposé en dev_code")

    if sent and sms_service.is_configured():
        message = "Code envoyé par SMS"
    else:
        message = "SMS indisponible, utilisez le code de test"

    return OtpResponse(message=message, phone=phone, dev_code=dev_code)


@router.post("/verify-otp", response_model=TokenResponse)
async def verify_otp(body: VerifyOtpBody):
    is_valid = await verify_otp_code(body.phone, body.code)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Code invalide ou expiré",
        )

    user = await user_service.get_or_create_user(body.phone)
    token = create_access_token(user_id=str(user.id), phone=body.phone)

    return TokenResponse(
        access_token=token,
        token_type="bearer",
        user_id=str(user.id),
        phone=body.phone,
    )
