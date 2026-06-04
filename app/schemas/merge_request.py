import uuid
from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class MergeRequestCreate(BaseModel):
    source_tree_id: uuid.UUID
    target_tree_id: uuid.UUID
    source_person_id: Optional[uuid.UUID] = None
    target_person_id: Optional[uuid.UUID] = None


class MergeRequestResponse(BaseModel):
    id: str
    source_tree_id: str
    target_tree_id: str
    source_person_id: Optional[str] = None
    target_person_id: Optional[str] = None
    requested_by_user_id: str
    status: str  # pending | approved | rejected
    created_at: datetime
    reviewed_by_user_id: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    source_tree_name: Optional[str] = None
    target_tree_name: Optional[str] = None
    requester_first_name: Optional[str] = None
