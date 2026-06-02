"""
Cloudinary storage service for photos and audio clips.
"""

import logging
import io
import time
import asyncio
from typing import Optional, Dict, Any

import cloudinary
import cloudinary.uploader
import cloudinary.utils
import cloudinary.api

from app.config import settings

logger = logging.getLogger(__name__)

# Configure Cloudinary
cloudinary.config(
    cloud_name=settings.CLOUDINARY_CLOUD_NAME,
    api_key=settings.CLOUDINARY_API_KEY,
    api_secret=settings.CLOUDINARY_API_SECRET,
    secure=True,
)

def is_configured() -> bool:
    return bool(settings.CLOUDINARY_CLOUD_NAME and settings.CLOUDINARY_API_KEY
                and settings.CLOUDINARY_API_SECRET)


def sign_direct_upload(folder: str) -> Dict[str, Any]:
    """
    Produit les paramètres signés pour un upload DIRECT navigateur → Cloudinary.

    Le navigateur enverra le fichier (potentiellement très volumineux : un vocal
    de 45 min) directement à Cloudinary, sans transiter par le backend. On signe
    uniquement `folder` + `timestamp` ; le client doit envoyer EXACTEMENT ces
    paramètres, plus api_key. La signature expire (Cloudinary rejette un
    timestamp trop ancien), ce qui limite la fenêtre d'abus.
    """
    timestamp = int(time.time())
    params_to_sign = {"folder": folder, "timestamp": timestamp}
    signature = cloudinary.utils.api_sign_request(
        params_to_sign, settings.CLOUDINARY_API_SECRET
    )
    return {
        "cloud_name": settings.CLOUDINARY_CLOUD_NAME,
        "api_key": settings.CLOUDINARY_API_KEY,
        "timestamp": timestamp,
        "signature": signature,
        "folder": folder,
    }


async def get_resource(public_id: str, media_type: str) -> Optional[Dict[str, Any]]:
    """
    Vérifie qu'un asset existe réellement sur Cloudinary et retourne ses
    métadonnées autoritatives (url, bytes, duration). Empêche un client de créer
    une ligne Media pointant vers une URL arbitraire. Appel réseau bloquant →
    exécuté hors event loop.
    """
    resource_type = "image" if media_type == "photo" else "video"
    try:
        return await asyncio.to_thread(
            cloudinary.api.resource, public_id, resource_type=resource_type
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Asset Cloudinary introuvable ({public_id}): {exc}")
        return None


PHOTO_FOLDER = "jabot/photos"
AUDIO_FOLDER = "jabot/audio"


async def upload_to_cloudinary(
    file_content: bytes,
    filename: str,
    media_type: str,
    person_id: str,
) -> Optional[Dict[str, Any]]:
    """
    Upload file to Cloudinary.
    Returns dict with 'public_id', 'secure_url', 'duration' (for audio) or None on failure.
    """
    if not settings.CLOUDINARY_CLOUD_NAME or not settings.CLOUDINARY_API_KEY:
        if settings.ENVIRONMENT == "development":
            # Dev stub: return a fake result
            logger.info(f"[DEV] Stub Cloudinary upload for {filename} ({media_type})")
            stub_id = f"jabot/{media_type}/{person_id}/{filename}"
            return {
                "public_id": stub_id,
                "secure_url": f"https://res.cloudinary.com/stub/{media_type}/upload/{stub_id}",
                "duration": 30 if media_type == "audio" else None,
            }
        logger.error("Cloudinary non configuré")
        return None

    try:
        folder = PHOTO_FOLDER if media_type == "photo" else AUDIO_FOLDER
        resource_type = "image" if media_type == "photo" else "video"  # Cloudinary uses "video" for audio

        upload_options: Dict[str, Any] = {
            "folder": f"{folder}/{person_id}",
            "resource_type": resource_type,
            "use_filename": True,
            "unique_filename": True,
        }

        if media_type == "photo":
            # Auto-optimize images
            upload_options["transformation"] = [
                {"quality": "auto", "fetch_format": "auto"},
                {"width": 1200, "crop": "limit"},
            ]
        else:
            # Audio: keep original format
            upload_options["format"] = "mp3"

        # cloudinary.uploader.upload est un appel RÉSEAU SYNCHRONE bloquant. Avec
        # un seul worker uvicorn, l'exécuter directement gèle l'event loop pour
        # toute la durée de l'upload (un audio de plusieurs Mo via mobile peut
        # prendre des dizaines de secondes) : le health check Render expire et
        # TOUTES les autres requêtes (dont GET /tree) restent bloquées. On
        # l'exécute donc hors de l'event loop via un thread.
        result = await asyncio.to_thread(
            cloudinary.uploader.upload,
            io.BytesIO(file_content),
            **upload_options,
        )

        return {
            "public_id": result["public_id"],
            "secure_url": result["secure_url"],
            "duration": int(result.get("duration", 0)) or None,
        }

    except cloudinary.exceptions.Error as e:
        logger.error(f"Erreur Cloudinary: {e}")
        return None
    except Exception as e:
        logger.error(f"Erreur inattendue lors de l'upload Cloudinary: {e}", exc_info=True)
        return None


async def delete_from_cloudinary(public_id: str, media_type: str) -> bool:
    """
    Delete a resource from Cloudinary.
    Returns True on success.
    """
    if not settings.CLOUDINARY_CLOUD_NAME or not settings.CLOUDINARY_API_KEY:
        if settings.ENVIRONMENT == "development":
            logger.info(f"[DEV] Stub Cloudinary delete for {public_id}")
            return True
        return False

    try:
        resource_type = "image" if media_type == "photo" else "video"
        # Appel réseau synchrone bloquant → hors event loop (cf. upload).
        result = await asyncio.to_thread(
            cloudinary.uploader.destroy, public_id, resource_type=resource_type
        )
        if result.get("result") == "ok":
            logger.info(f"Ressource Cloudinary supprimée: {public_id}")
            return True
        else:
            logger.warning(f"Suppression Cloudinary a retourné: {result}")
            return False
    except Exception as e:
        logger.error(f"Erreur lors de la suppression Cloudinary {public_id}: {e}")
        return False
