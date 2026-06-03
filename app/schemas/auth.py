import uuid
from typing import Optional
from pydantic import BaseModel, field_validator
import re


class OTPRequest(BaseModel):
    phone: str

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        # Normalize: strip spaces, ensure + prefix
        phone = re.sub(r"\s+", "", v)
        if not phone.startswith("+"):
            phone = "+" + phone
        if not re.match(r"^\+[1-9]\d{6,14}$", phone):
            raise ValueError("Numero de telephone invalide")
        return phone


class OTPVerify(BaseModel):
    phone: str
    code: str

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        phone = re.sub(r"\s+", "", v)
        if not phone.startswith("+"):
            phone = "+" + phone
        return phone

    @field_validator("code")
    @classmethod
    def validate_code(cls, v: str) -> str:
        if not re.match(r"^\d{6}$", v):
            raise ValueError("Le code OTP doit contenir 6 chiffres")
        return v


class TreeAccessItem(BaseModel):
    tree_id: str
    tree_name: str
    role: str
    created_at: Optional[str] = None


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    phone: str
    # person_id non nul => l'utilisateur a deja fait son onboarding.
    person_id: Optional[str] = None
    onboarded: bool = False
    # Arbres auxquels l'utilisateur a accès + arbre actif par défaut.
    tree_accesses: list[TreeAccessItem] = []
    active_tree_id: Optional[str] = None


class MeResponse(BaseModel):
    user_id: str
    phone: str
    person_id: Optional[str] = None
    onboarded: bool = False
    # Token reemis (session glissante) : le frontend le stocke pour repousser
    # l'expiration. Absent si rien a renouveler.
    access_token: Optional[str] = None
    tree_accesses: list[TreeAccessItem] = []
    active_tree_id: Optional[str] = None


class LinkPersonRequest(BaseModel):
    """C'est moi : rattache l'utilisateur a une fiche existante du canvas."""
    person_id: uuid.UUID
    # Arbre où se trouve la fiche (multi-arbre). Optionnel : déduit de la fiche.
    tree_id: Optional[uuid.UUID] = None


# ─── Recherche d'onboarding multi-arbres ────────────────────────────

class OnboardSearchRequest(BaseModel):
    name: Optional[str] = None
    nickname: Optional[str] = None
    birth_date: Optional[str] = None
    parent_names: Optional[list[str]] = None
    sibling_names: Optional[list[str]] = None
    city_of_origin: Optional[str] = None


class MatchRelative(BaseModel):
    first_name: str
    last_name: Optional[str] = None


class OnboardMatch(BaseModel):
    """Une correspondance trouvée dans un arbre, avec le contexte familial
    immédiat (parents + fratrie) pour que l'utilisateur reconnaisse sa famille."""
    tree_id: str
    tree_name: str
    person_id: str
    first_name: str
    last_name: Optional[str] = None
    birth_date: Optional[str] = None
    confidence: float
    parents: list[MatchRelative] = []
    siblings: list[MatchRelative] = []


class OnboardSearchResponse(BaseModel):
    matches: list[OnboardMatch] = []
