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
    created_at: datetime

    model_config = {"from_attributes": True}


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
