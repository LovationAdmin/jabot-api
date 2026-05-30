"""
Cloudinary storage service for photos and audio clips.
"""

import logging
import io
from typing import Optional, Dict, Any

import cloudinary
import cloudinary.uploader

from app.config import settings

logger = logging.getLogger(__name__)

# Configure Cloudinary
cloudinary.config(
    cloud_name=settings.CLOUDINARY_CLOUD_NAME,
    api_key=settings.CLOUDINARY_API_KEY,
    api_secret=settings.CLOUDINARY_API_SECRET,
    secure=True,
)

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

        result = cloudinary.uploader.upload(
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
        result = cloudinary.uploader.destroy(public_id, resource_type=resource_type)
        if result.get("result") == "ok":
            logger.info(f"Ressource Cloudinary supprimée: {public_id}")
            return True
        else:
            logger.warning(f"Suppression Cloudinary a retourné: {result}")
            return False
    except Exception as e:
        logger.error(f"Erreur lors de la suppression Cloudinary {public_id}: {e}")
        return False
