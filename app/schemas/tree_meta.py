import uuid
from typing import Optional
from pydantic import BaseModel


class TreeAccessResponse(BaseModel):
    tree_id: str
    tree_name: str
    role: str
    created_at: Optional[str] = None


class TreeListResponse(BaseModel):
    trees: list[TreeAccessResponse]


class TreeCreateRequest(BaseModel):
    name: Optional[str] = None


class TreeRenameRequest(BaseModel):
    name: str


class TreeMemberResponse(BaseModel):
    user_id: str
    role: str
    person_name: Optional[str] = None


class MemberRoleUpdate(BaseModel):
    role: str  # 'member' | 'visitor' | 'owner'
