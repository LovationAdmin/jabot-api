import uuid
from datetime import datetime
from pydantic import BaseModel, field_validator


VALID_TYPES = ("parent", "child", "sibling", "spouse")


class RelationshipCreate(BaseModel):
    person_a_id: uuid.UUID
    person_b_id: uuid.UUID
    type: str

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        if v not in VALID_TYPES:
            raise ValueError(
                f"Le type de relation doit être l'un de: {', '.join(VALID_TYPES)}"
            )
        return v


class RelationshipResponse(BaseModel):
    id: uuid.UUID
    person_a_id: uuid.UUID
    person_b_id: uuid.UUID
    type: str
    created_at: datetime

    model_config = {"from_attributes": True}
