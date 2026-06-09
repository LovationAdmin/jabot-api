"""
SMS dispatch service — cascade de fournisseurs par coût et couverture.

Ordre de priorité :
  1. Termii       — spécialiste OTP Afrique (Sénégal, CI…), ~$0.03-0.08/SMS
  2. Brevo        — le moins cher pour la France/EU, ~€0.045/SMS
  3. Africa's Talking — fallback Afrique (si compte toujours actif)
  4. Vonage       — dernier recours mondial

Chaque fournisseur est tenté seulement si ses clés sont configurées.
En mode dev (SMS_DEV_MODE=True) aucun appel réseau n'est émis et le code
est retourné en clair dans la réponse /request-otp.
"""
import httpx
import logging
from app.config import settings

logger = logging.getLogger(__name__)

# ─── Endpoints ────────────────────────────────────────────────────────────────

TERMII_SMS_URL         = "https://api.ng.termii.com/api/sms/send"
BREVO_SMS_URL          = "https://api.brevo.com/v3/transactionalSMS/sms"
AFRICAS_TALKING_URL    = "https://api.africastalking.com/version1/messaging"
VONAGE_SMS_URL         = "https://rest.nexmo.com/sms/json"

# ─── Availability checks ──────────────────────────────────────────────────────

def _termii_configured() -> bool:
    return bool(settings.TERMII_API_KEY)

def _brevo_configured() -> bool:
    return bool(settings.BREVO_API_KEY)

def _africas_talking_configured() -> bool:
    return bool(settings.AFRICAS_TALKING_API_KEY)

def _vonage_configured() -> bool:
    return bool(settings.VONAGE_API_KEY and settings.VONAGE_API_SECRET)

def is_configured() -> bool:
    if settings.SMS_DEV_MODE:
        return False
    return any([
        _termii_configured(),
        _brevo_configured(),
        _africas_talking_configured(),
        _vonage_configured(),
    ])

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _normalize_e164(phone: str) -> str:
    """Strips spaces/dashes, keeps the leading +."""
    return phone.replace(" ", "").replace("-", "")

def _strip_plus(phone: str) -> str:
    return _normalize_e164(phone).lstrip("+")

# ─── Provider implementations ─────────────────────────────────────────────────

async def _send_termii(phone: str, message: str) -> bool:
    """
    Termii generic SMS channel — sends our pre-generated OTP code.
    Docs: https://developers.termii.com/messaging
    """
    payload = {
        "api_key": settings.TERMII_API_KEY,
        "to": _normalize_e164(phone),
        "from": settings.TERMII_SENDER_ID,
        "sms": message,
        "type": "plain",
        "channel": "generic",
    }
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            r = await client.post(TERMII_SMS_URL, json=payload)
            r.raise_for_status()
            data = r.json()
            # Termii returns {"message": "Successfully Sent", "message_id": "...", ...}
            msg = data.get("message", "")
            if "successfully" in msg.lower():
                logger.info(f"[Termii] SMS envoyé à {phone}")
                return True
            logger.error(f"[Termii] Échec pour {phone}: {data}")
            return False
    except httpx.HTTPStatusError as e:
        logger.error(f"[Termii] HTTP {e.response.status_code} pour {phone}: {e.response.text}")
        return False
    except Exception as e:
        logger.error(f"[Termii] Erreur pour {phone}: {e}")
        return False


async def _send_brevo(phone: str, message: str) -> bool:
    """
    Brevo (ex-Sendinblue) transactional SMS.
    Docs: https://developers.brevo.com/reference/sendtransacsms
    """
    payload = {
        "sender": settings.BREVO_SENDER,
        "recipient": _normalize_e164(phone),
        "content": message,
        "type": "transactional",
    }
    headers = {
        "api-key": settings.BREVO_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            r = await client.post(BREVO_SMS_URL, json=payload, headers=headers)
            if r.status_code in (200, 201):
                logger.info(f"[Brevo] SMS envoyé à {phone}")
                return True
            logger.error(f"[Brevo] HTTP {r.status_code} pour {phone}: {r.text}")
            return False
    except httpx.HTTPStatusError as e:
        logger.error(f"[Brevo] HTTP {e.response.status_code} pour {phone}: {e.response.text}")
        return False
    except Exception as e:
        logger.error(f"[Brevo] Erreur pour {phone}: {e}")
        return False


async def _send_africas_talking(phone: str, message: str) -> bool:
    headers = {
        "apiKey": settings.AFRICAS_TALKING_API_KEY,
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    data: dict = {
        "username": settings.AFRICAS_TALKING_USERNAME,
        "to": phone,
        "message": message,
    }
    if settings.AFRICAS_TALKING_SENDER_ID:
        data["from"] = settings.AFRICAS_TALKING_SENDER_ID
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            r = await client.post(AFRICAS_TALKING_URL, headers=headers, data=data)
            r.raise_for_status()
            result = r.json()
            recipients = result.get("SMSMessageData", {}).get("Recipients", [])
            if recipients and recipients[0].get("status") == "Success":
                logger.info(f"[Africa's Talking] SMS envoyé à {phone}")
                return True
            logger.error(f"[Africa's Talking] Échec pour {phone}: {result}")
            return False
    except Exception as e:
        logger.error(f"[Africa's Talking] Erreur pour {phone}: {e}")
        return False


async def _send_vonage(phone: str, message: str) -> bool:
    data = {
        "api_key": settings.VONAGE_API_KEY,
        "api_secret": settings.VONAGE_API_SECRET,
        "to": _strip_plus(phone),
        "from": settings.VONAGE_BRAND_NAME,
        "text": message,
        "type": "unicode",
    }
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            r = await client.post(VONAGE_SMS_URL, data=data)
            r.raise_for_status()
            result = r.json()
            messages = result.get("messages", [])
            if messages and messages[0].get("status") == "0":
                logger.info(f"[Vonage] SMS envoyé à {phone}")
                return True
            err = messages[0].get("error-text") if messages else result
            logger.error(f"[Vonage] Échec pour {phone}: {err}")
            return False
    except Exception as e:
        logger.error(f"[Vonage] Erreur pour {phone}: {e}")
        return False

# ─── Public API ───────────────────────────────────────────────────────────────

async def send_sms(phone: str, message: str) -> bool:
    if settings.SMS_DEV_MODE:
        logger.info(f"[SMS_DEV_MODE] SMS to {phone}: {message}")
        return True

    providers = [
        ("Termii",          _termii_configured,          _send_termii),
        ("Brevo",           _brevo_configured,           _send_brevo),
        ("Africa's Talking",_africas_talking_configured, _send_africas_talking),
        ("Vonage",          _vonage_configured,          _send_vonage),
    ]

    for name, is_ready, send_fn in providers:
        if not is_ready():
            continue
        if await send_fn(phone, message):
            return True
        logger.warning(f"[SMS] {name} a échoué, tentative avec le fournisseur suivant")

    logger.error("[SMS] Tous les fournisseurs ont échoué ou aucun n'est configuré")
    return False


async def send_otp_sms(phone: str, otp_code: str) -> bool:
    # Libellé identique au usecase enregistré chez Termii pour le sender ID
    # « Lovation » — ne pas le modifier sans mettre à jour l'enregistrement.
    message = (
        f"Votre code de vérification Lovation : {otp_code}. "
        f"Valable 10 minutes. Ne le partagez avec personne."
    )
    return await send_sms(phone, message)
