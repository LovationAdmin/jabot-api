import uuid
from typing import List, Optional
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


class MergePair(BaseModel):
    """A confirmed (source_person_id, target_person_id) pair to merge during convergence."""
    source_person_id: uuid.UUID
    target_person_id: uuid.UUID


class TreeConvergeRequest(BaseModel):
    """Fusionne l'arbre source (dont l'appelant est propriétaire) dans l'arbre
    cible (où il a été invité). Les fiches d'identité confirmées sont fusionnées."""
    source_tree_id: uuid.UUID
    # Fiche de l'utilisateur dans l'arbre source (sa fiche actuelle).
    source_person_id: Optional[uuid.UUID] = None
    # Fiche correspondante dans l'arbre cible (« c'est moi »).
    target_person_id: Optional[uuid.UUID] = None
    # Paires supplémentaires confirmées par l'utilisateur via le scan pré-convergence.
    additional_merge_pairs: Optional[List[MergePair]] = None


class TreeConvergeResponse(BaseModel):
    message: str
    source_tree_id: str
    target_tree_id: str
    persons_moved: int
    identity_merged: bool
    additional_merges: int = 0


# ─── Pre-convergence scan ───────────────────────────────────────────────────

class CrossTreeMatchPair(BaseModel):
    """A proposed match between a person in the source tree and one in the target tree."""
    source_person_id: str
    source_first_name: str
    source_last_name: Optional[str] = None
    target_person_id: str
    target_first_name: str
    target_last_name: Optional[str] = None
    confidence: float
    match_reasons: List[str]
    # 'first_name' | 'full_name' | 'full_name_context'
    match_stage: str


class PreConvergeScanRequest(BaseModel):
    source_tree_id: uuid.UUID


class PreConvergeScanResponse(BaseModel):
    proposed_pairs: List[CrossTreeMatchPair]
    # Number of source persons with no confident match in target tree
    unmatched_source_count: int
