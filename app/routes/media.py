import uuid
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.media import Media
from app.models.person import Person
from app.schemas.media import (
    MediaResponse, MediaSignRequest, MediaSignResponse, MediaConfirmRequest,
)
from app.middleware.auth import get_current_user
from app.models.user import User
from app.services.storage_service import (
    upload_to_cloudinary, delete_from_cloudinary,
    is_configured, sign_direct_upload, get_resource, PHOTO_FOLDER, AUDIO_FOLDER,
)
from app.services.ws_manager import manager as ws_manager
from app.services.tree_cache import invalidate_tree_cache

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_PHOTOS_PER_PERSON = 10
MAX_AUDIOS_PER_PERSON = 10
# Un vocal peut durer jusqu'à 45 min. À ~32 kbps mono (voix) ≈ 11 Mo, mais selon
# le codec/navigateur le débit peut être plus élevé : on garde une marge large.
MAX_AUDIO_SIZE_BYTES = 200 * 1024 * 1024  # 200 MB
MAX_PHOTO_SIZE_BYTES = 25 * 1024 * 1024   # 25 MB

ALLOWED_IMAGE_TYPES = {
    "image/jpeg", "image/jpg", "image/png", "image/gif", "image/webp"
}
ALLOWED_AUDIO_TYPES = {
    "audio/mpeg", "audio/mp3", "audio/wav", "audio/ogg", "audio/aac",
    "audio/x-m4a", "audio/mp4", "audio/webm",
}
# Base MIME types accepted for audio (codec params stripped before check)
ALLOWED_AUDIO_BASE_TYPES = ALLOWED_AUDIO_TYPES


@router.post("/upload", response_model=MediaResponse, status_code=status.HTTP_201_CREATED)
async def upload_media(
    person_id: uuid.UUID = Form(...),
    media_type: str = Form(..., description="'photo' ou 'audio'"),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Upload une photo ou un audio vers Cloudinary.
    - Photos: max 3 par personne, types image uniquement
    - Audio: max 3 par personne, max 50 Mo chacun
    """
    if media_type not in ("photo", "audio"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Le type de média doit être 'photo' ou 'audio'",
        )

    # Verify person exists
    person_result = await db.execute(
        select(Person).where(Person.id == person_id, Person.deleted_at.is_(None))
    )
    if person_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Personne introuvable")

    # Validate file type — strip codec params before matching (e.g. "audio/mp4;codecs=mp4a.40.2")
    raw_ct = (file.content_type or "").strip()
    base_ct = raw_ct.split(";")[0].strip().lower()
    if media_type == "photo" and base_ct not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Type de fichier non supporté. Types acceptés: {', '.join(ALLOWED_IMAGE_TYPES)}",
        )
    if media_type == "audio" and base_ct not in ALLOWED_AUDIO_BASE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Type de fichier audio non supporté. Types acceptés: {', '.join(sorted(ALLOWED_AUDIO_BASE_TYPES))}",
        )

    # Check count limit
    count_result = await db.execute(
        select(func.count(Media.id)).where(
            Media.person_id == person_id, Media.type == media_type
        )
    )
    current_count = count_result.scalar_one()
    max_count = MAX_PHOTOS_PER_PERSON if media_type == "photo" else MAX_AUDIOS_PER_PERSON
    if current_count >= max_count:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Maximum {max_count} {media_type}(s) autorisé(s) par personne",
        )

    # Read file content
    file_content = await file.read()
    file_size = len(file_content)

    # Check audio size limit
    if media_type == "audio" and file_size > MAX_AUDIO_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Le fichier audio ne doit pas dépasser {MAX_AUDIO_SIZE_BYTES // (1024*1024)} Mo",
        )

    # Upload to Cloudinary
    upload_result = await upload_to_cloudinary(
        file_content=file_content,
        filename=file.filename or "upload",
        media_type=media_type,
        person_id=str(person_id),
    )
    if upload_result is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Échec du téléversement vers le service de stockage",
        )

    # Determine order_index
    order_result = await db.execute(
        select(func.coalesce(func.max(Media.order_index), -1)).where(
            Media.person_id == person_id, Media.type == media_type
        )
    )
    next_order = order_result.scalar_one() + 1

    media_record = Media(
        id=uuid.uuid4(),
        person_id=person_id,
        type=media_type,
        cloudinary_id=upload_result["public_id"],
        url=upload_result["secure_url"],
        duration_seconds=upload_result.get("duration"),
        file_size_bytes=file_size,
        order_index=next_order,
        uploaded_by_user_id=current_user.id,
    )
    db.add(media_record)
    await db.commit()
    await invalidate_tree_cache()
    await ws_manager.broadcast("media.changed", {"person_id": str(person_id)}, str(current_user.id))
    result = await db.execute(
        select(Media)
        .options(selectinload(Media.uploaded_by).selectinload(User.person))
        .where(Media.id == media_record.id)
    )
    return MediaResponse.from_orm_with_uploader(result.scalar_one())


async def _check_count_limit(db: AsyncSession, person_id: uuid.UUID, media_type: str) -> None:
    """Vérifie que la personne n'a pas atteint le quota de médias de ce type."""
    count_result = await db.execute(
        select(func.count(Media.id)).where(
            Media.person_id == person_id, Media.type == media_type
        )
    )
    current_count = count_result.scalar_one()
    max_count = MAX_PHOTOS_PER_PERSON if media_type == "photo" else MAX_AUDIOS_PER_PERSON
    if current_count >= max_count:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Maximum {max_count} {media_type}(s) autorisé(s) par personne",
        )


@router.post("/sign", response_model=MediaSignResponse)
async def sign_media_upload(
    body: MediaSignRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Autorise un upload DIRECT navigateur → Cloudinary et renvoie une signature.

    Le fichier (photo ou vocal jusqu'à 45 min) ne transite JAMAIS par le backend :
    le navigateur l'envoie directement à Cloudinary, ce qui évite les limites de
    taille/timeout de requête, ne bloque pas le worker unique et divise la bande
    passante par deux. Le backend se contente d'autoriser (auth + quota) puis de
    signer, et enregistrera les métadonnées via POST /media une fois l'upload fini.
    """
    if body.media_type not in ("photo", "audio"):
        raise HTTPException(status_code=400, detail="media_type doit être 'photo' ou 'audio'")
    if not is_configured():
        logger.error("[sign] Cloudinary non configuré (CLOUDINARY_CLOUD_NAME/API_KEY/API_SECRET manquants)")
        raise HTTPException(status_code=503, detail="Stockage média non configuré")

    person_result = await db.execute(
        select(Person).where(Person.id == body.person_id, Person.deleted_at.is_(None))
    )
    if person_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Personne introuvable")

    await _check_count_limit(db, body.person_id, body.media_type)

    base = PHOTO_FOLDER if body.media_type == "photo" else AUDIO_FOLDER
    folder = f"{base}/{body.person_id}"
    signed = sign_direct_upload(folder)
    resource_type = "image" if body.media_type == "photo" else "video"
    return MediaSignResponse(resource_type=resource_type, **signed)


@router.post("", response_model=MediaResponse, status_code=status.HTTP_201_CREATED)
async def confirm_media_upload(
    body: MediaConfirmRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Enregistre les métadonnées d'un média après un upload direct réussi.

    Le serveur INTERROGE Cloudinary pour récupérer l'URL/poids/durée autoritatifs
    (le client ne peut donc pas injecter une URL arbitraire) et applique les
    limites de taille avant de persister.
    """
    if body.media_type not in ("photo", "audio"):
        raise HTTPException(status_code=400, detail="media_type doit être 'photo' ou 'audio'")

    person_result = await db.execute(
        select(Person).where(Person.id == body.person_id, Person.deleted_at.is_(None))
    )
    if person_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Personne introuvable")

    await _check_count_limit(db, body.person_id, body.media_type)

    info = await get_resource(body.public_id, body.media_type)
    if info is None:
        raise HTTPException(status_code=400, detail="Asset introuvable sur le stockage")

    file_size = info.get("bytes")
    max_size = MAX_PHOTO_SIZE_BYTES if body.media_type == "photo" else MAX_AUDIO_SIZE_BYTES
    if file_size and file_size > max_size:
        # Trop volumineux : on supprime l'asset et on refuse.
        await delete_from_cloudinary(body.public_id, body.media_type)
        raise HTTPException(
            status_code=400,
            detail=f"Fichier trop volumineux (max {max_size // (1024*1024)} Mo)",
        )

    order_result = await db.execute(
        select(func.coalesce(func.max(Media.order_index), -1)).where(
            Media.person_id == body.person_id, Media.type == body.media_type
        )
    )
    next_order = order_result.scalar_one() + 1

    media_record = Media(
        id=uuid.uuid4(),
        person_id=body.person_id,
        type=body.media_type,
        cloudinary_id=body.public_id,
        url=info["secure_url"],
        duration_seconds=int(info.get("duration", 0)) or None,
        file_size_bytes=file_size,
        order_index=next_order,
        uploaded_by_user_id=current_user.id,
    )
    db.add(media_record)
    await db.commit()
    await invalidate_tree_cache()
    await ws_manager.broadcast("media.changed", {"person_id": str(body.person_id)}, str(current_user.id))
    result = await db.execute(
        select(Media)
        .options(selectinload(Media.uploaded_by).selectinload(User.person))
        .where(Media.id == media_record.id)
    )
    return MediaResponse.from_orm_with_uploader(result.scalar_one())


@router.delete("/{media_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_media(
    media_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Supprime un média (authentification requise). Supprime aussi de Cloudinary."""
    result = await db.execute(select(Media).where(Media.id == media_id))
    media_record = result.scalar_one_or_none()
    if media_record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Média introuvable")

    # Delete from Cloudinary
    await delete_from_cloudinary(media_record.cloudinary_id, media_record.type)

    pid = str(media_record.person_id)
    await db.delete(media_record)
    await db.commit()
    await invalidate_tree_cache()
    await ws_manager.broadcast("media.changed", {"person_id": pid}, str(current_user.id))
