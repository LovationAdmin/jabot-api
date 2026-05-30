import httpx
import logging
from app.config import settings

logger = logging.getLogger(__name__)

VONAGE_SMS_URL = "https://rest.nexmo.com/sms/json"
AFRICAS_TALKING_URL = "https://api.africastalking.com/version1/messaging"


def _vonage_configured() -> bool:
    return bool(settings.VONAGE_API_KEY and settings.VONAGE_API_SECRET)


def _africas_talking_configured() -> bool:
    return bool(settings.AFRICAS_TALKING_API_KEY)


def is_configured() -> bool:
    """True si au moins un fournisseur SMS réel est configuré."""
    return _vonage_configured() or _africas_talking_configured()


def _normalize_msisdn(phone: str) -> str:
    """Format E.164 sans le '+' (attendu par Vonage): +221 77 -> 22177."""
    return phone.replace(" ", "").replace("-", "").lstrip("+")


async def _send_vonage(phone: str, message: str) -> bool:
    """Envoi SMS via l'API REST Vonage (Nexmo)."""
    data = {
        "api_key": settings.VONAGE_API_KEY,
        "api_secret": settings.VONAGE_API_SECRET,
        "to": _normalize_msisdn(phone),
        "from": settings.VONAGE_BRAND_NAME,
        "text": message,
        "type": "unicode",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(VONAGE_SMS_URL, data=data)
            response.raise_for_status()
            result = response.json()
            messages = result.get("messages", [])
            if messages and messages[0].get("status") == "0":
                logger.info(f"SMS Vonage envoyé avec succès à {phone}")
                return True
            err = messages[0].get("error-text") if messages else "réponse vide"
            logger.error(f"Échec SMS Vonage à {phone}: {err}")
            return False
    except httpx.HTTPError as e:
        logger.error(f"Erreur HTTP Vonage à {phone}: {e}")
        return False
    except Exception as e:
        logger.error(f"Erreur inattendue Vonage à {phone}: {e}")
        return False


async def _send_africas_talking(phone: str, message: str) -> bool:
    """Envoi SMS via Africa's Talking (repli)."""
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
                logger.info(f"SMS Africa's Talking envoyé avec succès à {phone}")
                return True
            logger.error(f"Échec SMS Africa's Talking à {phone}: {result}")
            return False
    except httpx.HTTPError as e:
        logger.error(f"Erreur HTTP Africa's Talking à {phone}: {e}")
        return False
    except Exception as e:
        logger.error(f"Erreur inattendue Africa's Talking à {phone}: {e}")
        return False


async def send_sms(phone: str, message: str) -> bool:
    """Envoie un SMS via le premier fournisseur configuré (Vonage > Africa's Talking).

    Si aucun fournisseur n'est configuré, log le message (mode dev) et réussit.
    """
    if _vonage_configured():
        return await _send_vonage(phone, message)
    if _africas_talking_configured():
        return await _send_africas_talking(phone, message)

    logger.info(f"[DEV] SMS à {phone}: {message}")
    return True


async def send_otp_sms(phone: str, otp_code: str) -> bool:
    """Envoie le code OTP par SMS."""
    message = (
        f"Votre code de vérification JABOT est: {otp_code}\n"
        f"Ce code expire dans 10 minutes. Ne le partagez avec personne."
    )
    return await send_sms(phone, message)
