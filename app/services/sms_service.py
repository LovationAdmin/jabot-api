"""
SMS dispatch service — Termii uniquement.

Sender ID « Lovation » approuvé par Termii pour la route internationale
France + Sénégal (~$0.067/SMS France, ~$0.478/SMS Sénégal).

En mode dev (SMS_DEV_MODE=True) aucun appel réseau n'est émis et le code
est retourné en clair dans la réponse /request-otp.
"""
import httpx
import logging
from app.config import settings

logger = logging.getLogger(__name__)

TERMII_SMS_URL = "https://api.ng.termii.com/api/sms/send"


def _normalize_e164(phone: str) -> str:
    """Strips spaces/dashes, keeps the leading +."""
    return phone.replace(" ", "").replace("-", "")


async def _send_termii(phone: str, message: str) -> bool:
    """
    Termii generic SMS channel — sends our pre-generated codes.
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


# ─── Public API ───────────────────────────────────────────────────────────────

async def send_sms(phone: str, message: str) -> bool:
    if settings.SMS_DEV_MODE:
        logger.info(f"[SMS_DEV_MODE] SMS to {phone}: {message}")
        return True

    if not settings.TERMII_API_KEY:
        logger.error("[SMS] TERMII_API_KEY non configurée — envoi impossible")
        return False

    return await _send_termii(phone, message)


async def send_otp_sms(phone: str, otp_code: str) -> bool:
    # Libellé identique au usecase enregistré chez Termii pour le sender ID
    # « Lovation » — ne pas le modifier sans mettre à jour l'enregistrement.
    message = (
        f"Votre code de vérification Lovation : {otp_code}. "
        f"Valable 10 minutes. Ne le partagez avec personne."
    )
    return await send_sms(phone, message)
