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


class TreeConvergeRequest(BaseModel):
    """Fusionne l'arbre source (dont l'appelant est propriétaire) dans l'arbre
    cible (où il a été invité). Les fiches d'identité confirmées sont fusionnées."""
    source_tree_id: uuid.UUID
    # Fiche de l'utilisateur dans l'arbre source (sa fiche actuelle).
    source_person_id: Optional[uuid.UUID] = None
    # Fiche correspondante dans l'arbre cible (« c'est moi »).
    target_person_id: Optional[uuid.UUID] = None


class TreeConvergeResponse(BaseModel):
    message: str
    source_tree_id: str
    target_tree_id: str
    persons_moved: int
    identity_merged: bool

