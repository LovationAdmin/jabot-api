"""
Rate-limiting distribué (par IP) basé sur Redis.

Objectif : freiner le scraping massif et les abus (spam d'OTP) sans pénaliser
l'usage normal. Fenêtre fixe par (IP, bucket) via INCR + EXPIRE atomiques.

Principes de robustesse :
- Multi-instance : le compteur vit dans Redis, partagé entre tous les workers
  Render → la limite est globale, pas par process.
- Fail-open : si Redis est indisponible, on LAISSE PASSER la requête (on ne
  casse pas l'app pour un incident d'infra). Le rate-limiting est une défense
  en profondeur, pas un point de défaillance unique.
- Buckets dédiés : les endpoints sensibles (OTP, recherche, arbre complet) ont
  des limites plus strictes que la limite globale.
"""

import time
import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

import redis.asyncio as aioredis
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Rule:
    """Une règle = un préfixe de chemin, un nombre max de requêtes et une fenêtre (s)."""
    prefix: str
    limit: int
    window: int
    name: str


# Règles spécifiques (évaluées dans l'ordre, première correspondance retenue).
# Les chemins incluent le préfixe /api.
SPECIFIC_RULES: List[Rule] = [
    # OTP : très strict (anti-spam SMS / énumération de numéros).
    Rule("/api/auth/request-otp", limit=5,  window=60,  name="otp_min"),
    Rule("/api/auth/request-otp", limit=20, window=3600, name="otp_hour"),
    Rule("/api/auth/verify-otp",  limit=10, window=60,  name="verify_min"),
    # Recherche : modérément strict (vecteur d'extraction de noms).
    Rule("/api/persons/search", limit=30, window=60, name="search_min"),
    # Arbre complet : le plus gros payload, principal vecteur de scraping.
    Rule("/api/tree", limit=20, window=60, name="tree_min"),
]

# Limite globale par défaut (toute requête /api non couverte ci-dessus).
GLOBAL_RULE = Rule("/api", limit=120, window=60, name="global_min")


def _client_ip(request: Request) -> str:
    """
    IP réelle du client. Derrière le proxy Render, l'IP d'origine est le premier
    maillon de X-Forwarded-For. On retombe sur l'IP de connexion sinon.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rules_for(path: str) -> List[Rule]:
    """Règles applicables à un chemin : les règles spécifiques qui matchent + la globale."""
    matched = [r for r in SPECIFIC_RULES if path.startswith(r.prefix)]
    matched.append(GLOBAL_RULE)
    return matched


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self._redis: Optional[aioredis.Redis] = None

    def _client(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        return self._redis

    async def _check(self, ip: str, rule: Rule) -> Tuple[bool, int]:
        """
        Incrémente le compteur (IP, bucket) et indique si la limite est dépassée.
        Retourne (autorisé, retry_after_secondes). Fail-open si Redis KO.
        """
        # Fenêtre fixe alignée : clé qui change à chaque fenêtre.
        window_id = int(time.time()) // rule.window
        key = f"rl:{rule.name}:{ip}:{window_id}"
        try:
            redis = self._client()
            count = await redis.incr(key)
            if count == 1:
                await redis.expire(key, rule.window)
            if count > rule.limit:
                ttl = await redis.ttl(key)
                return False, ttl if ttl and ttl > 0 else rule.window
            return True, 0
        except Exception as e:  # noqa: BLE001 — fail-open volontaire
            logger.warning(f"Rate-limit indisponible (fail-open): {e}")
            return True, 0

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # On ne limite que l'API ; le health-check et la racine restent libres.
        if not path.startswith("/api"):
            return await call_next(request)
        # Les pré-vols CORS ne consomment pas de quota.
        if request.method == "OPTIONS":
            return await call_next(request)

        ip = _client_ip(request)
        for rule in _rules_for(path):
            allowed, retry_after = await self._check(ip, rule)
            if not allowed:
                logger.info(f"429 rate-limit {rule.name} ip={ip} path={path}")
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Trop de requêtes. Réessayez dans un instant."},
                    headers={"Retry-After": str(retry_after)},
                )

        return await call_next(request)
