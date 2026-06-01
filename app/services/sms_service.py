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
    # En mode dev SMS, on se comporte comme si aucun fournisseur n'etait
    # configure : la route OTP exposera alors le dev_code.
    if settings.SMS_DEV_MODE:
        return False
    return _vonage_configured() or _africas_talking_configured()


def _normalize_msisdn(phone: str) -> str:
    return phone.replace(" ", "").replace("-", "").lstrip("+")


async def _send_vonage(phone: str, message: str) -> bool:
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
                logger.info(f"SMS Vonage envoye avec succes a {phone}")
                return True
            err = messages[0].get("error-text") if messages else result
            logger.error(f"Echec Vonage a {phone}: {err}")
            return False
    except httpx.HTTPError as e:
        logger.error(f"Erreur HTTP Vonage a {phone}: {e}")
        return False
    except Exception as e:
        logger.error(f"Erreur inattendue Vonage a {phone}: {e}")
        return False


async def _send_africas_talking(phone: str, message: str) -> bool:
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
                logger.info(f"SMS Africa's Talking envoye avec succes a {phone}")
                return True
            logger.error(f"Echec Africa's Talking a {phone}: {result}")
            return False
    except httpx.HTTPError as e:
        logger.error(f"Erreur HTTP Africa's Talking a {phone}: {e}")
        return False
    except Exception as e:
        logger.error(f"Erreur inattendue Africa's Talking a {phone}: {e}")
        return False


async def send_sms(phone: str, message: str) -> bool:
    # Mode dev SMS : on n'appelle aucun fournisseur reel (pas de cout, pas de
    # dependance au trial Vonage). Le code sera expose via dev_code.
    if settings.SMS_DEV_MODE:
        logger.info(f"[SMS_DEV_MODE] SMS to {phone}: {message}")
        return True
    if _vonage_configured():
        return await _send_vonage(phone, message)
    if _africas_talking_configured():
        return await _send_africas_talking(phone, message)
    # Aucun fournisseur configuré et pas en mode dev : on NE simule PAS un
    # succès (sinon la route OTP croirait le SMS envoyé sans rien envoyer).
    logger.error("Aucun fournisseur SMS configuré et SMS_DEV_MODE désactivé")
    return False


async def send_otp_sms(phone: str, otp_code: str) -> bool:
    message = (
        f"Votre code de verification JABOT est: {otp_code}\n"
        f"Ce code expire dans 10 minutes. Ne le partagez avec personne."
    )
    return await send_sms(phone, message)
