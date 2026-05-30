from app.schemas.auth import OTPRequest, OTPVerify, Token
from app.schemas.person import PersonCreate, PersonUpdate, PersonResponse, PersonListResponse
from app.schemas.relationship import RelationshipCreate, RelationshipResponse
from app.schemas.media import MediaResponse

__all__ = [
    "OTPRequest", "OTPVerify", "Token",
    "PersonCreate", "PersonUpdate", "PersonResponse", "PersonListResponse",
    "RelationshipCreate", "RelationshipResponse",
    "MediaResponse",
]
