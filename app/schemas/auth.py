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


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    phone: str
    # person_id non nul => l'utilisateur a deja fait son onboarding.
    person_id: Optional[str] = None
    onboarded: bool = False


class MeResponse(BaseModel):
    user_id: str
    phone: str
    person_id: Optional[str] = None
    onboarded: bool = False


class LinkPersonRequest(BaseModel):
    """« C'est moi » : rattache l'utilisateur a une fiche existante du canvas."""
    person_id: uuid.UUID
