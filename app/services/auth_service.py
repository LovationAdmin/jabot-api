import random
import string
import uuid
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import redis.asyncio as aioredis
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import settings
from app.models.user import User

logger = logging.getLogger(__name__)

OTP_TTL_SECONDS = 600  # 10 minutes
OTP_KEY_PREFIX = "otp:"


def _get_redis_client() -> aioredis.Redis:
    return aioredis.from_url(settings.REDIS_URL, decode_responses=True)


def generate_otp() -> str:
    """Generate a 6-digit OTP code."""
    return "".join(random.choices(string.digits, k=6))


async def store_otp(phone: str, code: str) -> None:
    """Store OTP in Redis with 10-minute TTL."""
    async with _get_redis_client() as redis:
        key = f"{OTP_KEY_PREFIX}{phone}"
        await redis.setex(key, OTP_TTL_SECONDS, code)
        logger.info(f"OTP stocké pour {phone}")


async def verify_otp(phone: str, code: str) -> bool:
    """Verify OTP from Redis. Deletes on success."""
    async with _get_redis_client() as redis:
        key = f"{OTP_KEY_PREFIX}{phone}"
        stored = await redis.get(key)
        if stored is None:
            logger.warning(f"OTP expiré ou inexistant pour {phone}")
            return False
        if stored != code:
            logger.warning(f"OTP incorrect pour {phone}")
            return False
        await redis.delete(key)
        logger.info(f"OTP vérifié avec succès pour {phone}")
        return True


def create_access_token(user_id: str, phone: str) -> str:
    """Create a JWT access token."""
    expire = datetime.now(timezone.utc) + timedelta(days=settings.ACCESS_TOKEN_EXPIRE_DAYS)
    payload = {
        "sub": user_id,
        "phone": phone,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    """Decode and validate a JWT token."""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload
    except JWTError as e:
        logger.warning(f"Erreur de décodage JWT: {e}")
        return None


async def get_or_create_user(db: AsyncSession, phone: str) -> User:
    """Get existing user by phone or create a new one."""
    result = await db.execute(select(User).where(User.phone == phone))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(id=uuid.uuid4(), phone=phone)
        db.add(user)
        await db.commit()
        await db.refresh(user)
        logger.info(f"Nouvel utilisateur créé pour {phone}")
    return user
