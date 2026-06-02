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

_TTL = 30  # secondes


def _key(tree_id: str, authenticated: bool) -> str:
    return f"jabot:tree:{tree_id}:{'auth' if authenticated else 'anon'}"


def _redis() -> aioredis.Redis:
    # Timeouts courts : le cache ne doit JAMAIS bloquer la requête. Si Redis est
    # lent/injoignable, on échoue vite et on retombe sur le calcul direct.
    return aioredis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


async def get_tree_cache(tree_id: str, authenticated: bool) -> Optional[dict]:
    try:
        async with _redis() as r:
            raw = await r.get(_key(tree_id, authenticated))
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as exc:
        logger.debug(f"tree_cache get failed (fail-open): {exc}")
        return None


async def set_tree_cache(tree_id: str, authenticated: bool, data: dict) -> None:
    try:
        async with _redis() as r:
            await r.setex(_key(tree_id, authenticated), _TTL, json.dumps(data))
    except Exception as exc:
        logger.debug(f"tree_cache set failed (fail-open): {exc}")


async def invalidate_tree_cache(tree_id: Optional[str] = None) -> None:
    """Invalide le cache d'un arbre (ou de tous si tree_id est None).

    Appelé après toute mutation de l'arbre (fiche ou relation). Passer le
    tree_id concerné évite de purger les autres arbres inutilement.
    """
    try:
        async with _redis() as r:
            if tree_id is not None:
                await r.delete(_key(tree_id, True), _key(tree_id, False))
            else:
                # Purge globale (fallback) : supprime toutes les clés d'arbres.
                keys = [k async for k in r.scan_iter(match="jabot:tree:*")]
                if keys:
                    await r.delete(*keys)
    except Exception as exc:
        logger.debug(f"tree_cache invalidate failed (fail-open): {exc}")
