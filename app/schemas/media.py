import uuid
from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class MediaResponse(BaseModel):
    id: uuid.UUID
    person_id: uuid.UUID
    type: str
    cloudinary_id: str
    url: str
    duration_seconds: Optional[int] = None
    file_size_bytes: Optional[int] = None
    order_index: int
    uploader_name: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_with_uploader(cls, media) -> "MediaResponse":
        uploader_name = None
        if media.uploaded_by and media.uploaded_by.person:
            p = media.uploaded_by.person
            parts = [p.first_name, p.last_name] if hasattr(p, "last_name") else [p.first_name]
            uploader_name = " ".join(x for x in parts if x)
        return cls(
            id=media.id,
            person_id=media.person_id,
            type=media.type,
            cloudinary_id=media.cloudinary_id,
            url=media.url,
            duration_seconds=media.duration_seconds,
            file_size_bytes=media.file_size_bytes,
            order_index=media.order_index,
            uploader_name=uploader_name,
            created_at=media.created_at,
        )


class MediaSignRequest(BaseModel):
    """Demande de signature pour un upload direct navigateur → Cloudinary."""
    person_id: uuid.UUID
    media_type: str  # 'photo' | 'audio'


class MediaSignResponse(BaseModel):
    cloud_name: str
    api_key: str
    timestamp: int
    signature: str
    folder: str
    resource_type: str  # 'image' | 'video'


class MediaConfirmRequest(BaseModel):
    """Confirmation après upload direct : le client renvoie l'identifiant
    Cloudinary, le serveur vérifie l'asset et persiste la ligne Media."""
    person_id: uuid.UUID
    media_type: str
    public_id: str


class MergeRequest(BaseModel):
    source_person_id: uuid.UUID
    target_person_id: uuid.UUID
