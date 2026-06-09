"""
Quota d'envoi SMS par numéro de téléphone — conformité route Termii.

Termii (route internationale France/Sénégal) impose de limiter les renvois
vers un même numéro à DEUX par 24 h : au-delà, le trafic est classé spam
et facturé en conséquence. Ce module applique la règle côté application,
en complément du rate-limiting par IP (qui ne protège pas un numéro ciblé
depuis plusieurs IP).

Deux garde-fous, stockés dans Redis :
  - cooldown    : délai minimum entre deux envois vers le même numéro
                  (anti double-clic / re-soumission immédiate) ;
  - plafond 24h : nombre maximum d'envois vers un même numéro sur une
                  fenêtre de 24 h démarrant au premier envoi.

Les clés Redis utilisent le hash HMAC du numéro, jamais le numéro en clair.

Contrairement au rate-limit IP (fail-open), chaque SMS coûte de l'argent :
si Redis est injoignable on REFUSE l'envoi (fail-closed). L'OTP étant lui
aussi stocké dans Redis, le login serait de toute façon impossible.
"""
import logging
from dataclasses import dataclass

import redis.asyncio as aioredis

from app.config import settings
from app.security.crypto import phone_hash

logger = logging.getLogger(__name__)

DAY_SECONDS = 24 * 3600

COOLDOWN_PREFIX = "sms:cooldown:"
DAILY_PREFIX = "sms:daily:"


@dataclass(frozen=True)
class QuotaDecision:
    allowed: bool
    retry_after: int = 0  # secondes avant nouvelle tentative possible
    reason: str = ""      # "cooldown" | "daily_limit" | "unavailable"


def _get_redis_client() -> aioredis.Redis:
    return aioredis.from_url(settings.REDIS_URL, decode_responses=True)


def _keys(phone: str) -> tuple[str, str]:
    """Clés (cooldown, plafond 24 h) pour un numéro, normalisé puis haché
    pour que '+221 70…', '+221-70…' et '+22170…' partagent le même quota."""
    normalized = phone.replace(" ", "").replace("-", "")
    h = phone_hash(normalized)
    return f"{COOLDOWN_PREFIX}{h}", f"{DAILY_PREFIX}{h}"


async def reserve_send(phone: str) -> QuotaDecision:
    """Réserve un créneau d'envoi pour ce numéro. À appeler AVANT l'envoi.

    Si l'envoi échoue ensuite chez le fournisseur, rendre le créneau via
    release_send() pour ne pas consommer le quota de l'utilisateur.
    """
    cooldown_key, daily_key = _keys(phone)
    limit = settings.SMS_MAX_PER_PHONE_PER_DAY
    cooldown = settings.SMS_PHONE_COOLDOWN_SECONDS
    try:
        async with _get_redis_client() as redis:
            # 1) Cooldown anti double-clic (SET NX atomique).
            if cooldown > 0:
                created = await redis.set(cooldown_key, "1", nx=True, ex=cooldown)
                if not created:
                    ttl = await redis.ttl(cooldown_key)
                    return QuotaDecision(False, ttl if ttl > 0 else cooldown, "cooldown")

            # 2) Plafond 24 h : INCR atomique puis vérification.
            count = await redis.incr(daily_key)
            if count == 1:
                await redis.expire(daily_key, DAY_SECONDS)
            elif await redis.ttl(daily_key) == -1:
                # Clé sans TTL (EXPIRE perdu lors d'un crash) : on répare,
                # sinon le numéro resterait bloqué indéfiniment.
                await redis.expire(daily_key, DAY_SECONDS)
            if count > limit:
                await redis.decr(daily_key)  # tentative refusée ≠ envoi
                ttl = await redis.ttl(daily_key)
                return QuotaDecision(False, ttl if ttl > 0 else DAY_SECONDS, "daily_limit")

            return QuotaDecision(True)
    except Exception as e:  # noqa: BLE001 — fail-closed volontaire
        logger.error(f"Quota SMS indisponible, envoi refusé: {e}")
        return QuotaDecision(False, 30, "unavailable")


async def release_send(phone: str) -> None:
    """Rend le créneau réservé (échec d'envoi fournisseur) : l'utilisateur
    peut retenter immédiatement sans avoir consommé son quota."""
    cooldown_key, daily_key = _keys(phone)
    try:
        async with _get_redis_client() as redis:
            await redis.delete(cooldown_key)
            if await redis.decr(daily_key) < 0:
                await redis.delete(daily_key)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Libération du quota SMS impossible: {e}")
