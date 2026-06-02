"""
Cache Redis court-terme pour la réponse GET /api/tree.

Problème résolu : depuis la migration 005, chaque lecture de l'arbre déclenche
N déchiffrements Fernet (birth_date, death_date, city_of_origin × nb personnes)
dans les TypeDecorators SQLAlchemy — opérations synchrones qui s'exécutent sur
l'event loop. Sur un arbre de taille modeste, cela ajoute une latence perceptible
à chaque requête.

Stratégie :
- Deux clés : "anon" (visiteur non authentifié) et "auth" (utilisateur connecté),
  car les payloads diffèrent (les champs sensibles ne sont envoyés qu'aux auth).
- TTL de 30 s : courte durée qui plafonne la fenêtre d'incohérence visible.
- Invalidation immédiate sur toute mutation (ajout/modification/suppression de
  fiche ou de relation) → les auteurs voient leur changement sans délai.
- Fail-open : si Redis est indisponible, on saute le cache silencieusement et
  on recalcule à chaque appel (comportement antérieur).
"""
import json
import logging
from typing import Optional

import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger(__name__)

_CACHE_KEY_AUTH = "jabot:tree:auth"
_CACHE_KEY_ANON = "jabot:tree:anon"
_TTL = 30  # secondes


def _redis() -> aioredis.Redis:
    return aioredis.from_url(settings.REDIS_URL, decode_responses=True)


async def get_tree_cache(authenticated: bool) -> Optional[dict]:
    key = _CACHE_KEY_AUTH if authenticated else _CACHE_KEY_ANON
    try:
        async with _redis() as r:
            raw = await r.get(key)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as exc:
        logger.debug(f"tree_cache get failed (fail-open): {exc}")
        return None


async def set_tree_cache(authenticated: bool, data: dict) -> None:
    key = _CACHE_KEY_AUTH if authenticated else _CACHE_KEY_ANON
    try:
        async with _redis() as r:
            await r.setex(key, _TTL, json.dumps(data))
    except Exception as exc:
        logger.debug(f"tree_cache set failed (fail-open): {exc}")


async def invalidate_tree_cache() -> None:
    """Appelé après toute mutation de l'arbre (fiche ou relation)."""
    try:
        async with _redis() as r:
            await r.delete(_CACHE_KEY_AUTH, _CACHE_KEY_ANON)
    except Exception as exc:
        logger.debug(f"tree_cache invalidate failed (fail-open): {exc}")
