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


class MergeRequest(BaseModel):
    source_person_id: uuid.UUID
    target_person_id: uuid.UUID
