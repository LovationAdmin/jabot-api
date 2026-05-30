import httpx
import logging
from app.config import settings

logger = logging.getLogger(__name__)

AFRICAS_TALKING_URL = "https://api.africastalking.com/version1/messaging"


async def send_sms(phone: str, message: str) -> bool:
    """Send an SMS via Africa's Talking API."""
    if settings.ENVIRONMENT == "development" and not settings.AFRICAS_TALKING_API_KEY:
        logger.info(f"[DEV] SMS to {phone}: {message}")
        return True

    headers = {
        "apiKey": settings.AFRICAS_TALKING_API_KEY,
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    data = {
        "username": settings.AFRICAS_TALKING_USERNAME,
        "to": phone,
        "message": message,
    }
    if settings.AFRICAS_TALKING_SENDER_ID:
        data["from"] = settings.AFRICAS_TALKING_SENDER_ID

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(AFRICAS_TALKING_URL, headers=headers, data=data)
            response.raise_for_status()
            result = response.json()
            recipients = result.get("SMSMessageData", {}).get("Recipients", [])
            if recipients and recipients[0].get("status") == "Success":
                logger.info(f"SMS envoyé avec succès à {phone}")
                return True
            else:
                logger.error(f"Échec d'envoi SMS à {phone}: {result}")
                return False
    except httpx.HTTPError as e:
        logger.error(f"Erreur HTTP lors de l'envoi SMS à {phone}: {e}")
        return False
    except Exception as e:
        logger.error(f"Erreur inattendue lors de l'envoi SMS à {phone}: {e}")
        return False


async def send_otp_sms(phone: str, otp_code: str) -> bool:
    """Send OTP code via SMS."""
    message = (
        f"Votre code de vérification JABOT est: {otp_code}\n"
        f"Ce code expire dans 10 minutes. Ne le partagez avec personne."
    )
    return await send_sms(phone, message)
