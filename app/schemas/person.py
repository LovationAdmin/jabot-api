import uuid
from datetime import datetime, date
from typing import Optional, List, Any
from pydantic import BaseModel, field_validator


class CanvasPositionSchema(BaseModel):
    x: float = 0.0
    y: float = 0.0
    generation: int = 0

    model_config = {"from_attributes": True}


class MediaResponseMinimal(BaseModel):
    id: uuid.UUID
    type: str
    url: str
    order_index: int

    model_config = {"from_attributes": True}


class PersonCreate(BaseModel):
    first_name: str
    last_name: Optional[str] = None
    nicknames: Optional[List[str]] = None
    gender: Optional[str] = None
    birth_date: Optional[date] = None
    death_date: Optional[date] = None
    city_of_origin: Optional[str] = None

    @field_validator("gender")
    @classmethod
    def validate_gender(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("male", "female", "unknown"):
            raise ValueError("Le genre doit être 'male', 'female' ou 'unknown'")
        return v


class PersonUpdate(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    nicknames: Optional[List[str]] = None
    gender: Optional[str] = None
    birth_date: Optional[date] = None
    death_date: Optional[date] = None
    city_of_origin: Optional[str] = None

    @field_validator("gender")
    @classmethod
    def validate_gender(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("male", "female", "unknown"):
            raise ValueError("Le genre doit être 'male', 'female' ou 'unknown'")
        return v


class PersonResponse(BaseModel):
    id: uuid.UUID
    first_name: str
    last_name: Optional[str] = None
    nicknames: Optional[List[str]] = None
    gender: Optional[str] = None
    birth_date: Optional[date] = None
    death_date: Optional[date] = None
    city_of_origin: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    canvas_position: Optional[CanvasPositionSchema] = None
    media: Optional[List[MediaResponseMinimal]] = None

    model_config = {"from_attributes": True}


class PersonListResponse(BaseModel):
    total: int
    persons: List[PersonResponse]


class SearchRequest(BaseModel):
    name: Optional[str] = None
    nickname: Optional[str] = None
    parent_names: Optional[List[str]] = None
    sibling_names: Optional[List[str]] = None
    city_of_origin: Optional[str] = None


class SearchMatch(BaseModel):
    person: PersonResponse
    confidence: float
    match_reasons: List[str]
