"""
Synchronisation temps réel de l'arbre via WebSocket.

Objectif : quand un utilisateur modifie l'arbre (ajout/édition/suppression de
fiche, lien, média), tous les clients connectés reçoivent un événement et
rafraîchissent leur vue — évite les vues divergentes et les écrasements de
modifications concurrentes.

Architecture multi-instance : les événements transitent par Redis pub/sub. Un
backend qui reçoit une mutation publie sur le canal Redis ; chaque instance,
abonnée au canal, relaie l'événement à ses propres connexions WebSocket. Ainsi
la synchro fonctionne même derrière plusieurs workers / replicas.

Fail-open : si Redis est indisponible, la diffusion locale (même process)
continue de fonctionner ; seule la propagation inter-instances est perdue.
"""
import asyncio
import json
import logging
from typing import Set, Optional

import redis.asyncio as aioredis
from fastapi import WebSocket

from app.config import settings

logger = logging.getLogger(__name__)

TREE_CHANNEL = "jabot:tree:events"


class ConnectionManager:
    """Gère les connexions WebSocket locales + le relais via Redis pub/sub."""

    def __init__(self) -> None:
        # Connexions groupées par arbre (room). Une connexion sans arbre connu
        # est rangée sous la clé "" (diffusion globale héritée).
        self._rooms: dict[str, Set[WebSocket]] = {}
        self._ws_room: dict[WebSocket, str] = {}
        self._lock = asyncio.Lock()
        self._redis: Optional[aioredis.Redis] = None
        self._pubsub_task: Optional[asyncio.Task] = None

    async def startup(self) -> None:
        """Démarre l'abonnement Redis (relais inter-instances)."""
        try:
            self._redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            self._pubsub_task = asyncio.create_task(self._listen_redis())
            logger.info("WebSocket ConnectionManager: abonné au canal Redis")
        except Exception as exc:
            logger.warning(f"WebSocket: Redis indisponible, mode mono-instance ({exc})")

    async def shutdown(self) -> None:
        # Ferme proprement les connexions WebSocket pour ne pas faire traîner
        # l'arrêt graceful d'uvicorn (sinon Render attend le timeout complet à
        # chaque redéploiement, ce qui ralentit fortement les déploiements).
        async with self._lock:
            sockets = list(self._ws_room.keys())
            self._rooms.clear()
            self._ws_room.clear()
        for ws in sockets:
            try:
                await ws.close(code=1001)  # 1001 = going away
            except Exception:
                pass

        if self._pubsub_task:
            self._pubsub_task.cancel()
            try:
                await asyncio.wait_for(self._pubsub_task, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception:
                pass
        if self._redis:
            try:
                await self._redis.aclose()
            except Exception:
                pass

    async def connect(self, ws: WebSocket, tree_id: Optional[str] = None) -> None:
        await ws.accept()
        room = tree_id or ""
        async with self._lock:
            self._rooms.setdefault(room, set()).add(ws)
            self._ws_room[ws] = room

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            room = self._ws_room.pop(ws, None)
            if room is not None and room in self._rooms:
                self._rooms[room].discard(ws)
                if not self._rooms[room]:
                    del self._rooms[room]

    async def _listen_redis(self) -> None:
        """Boucle d'écoute Redis : relaie les événements aux connexions locales."""
        assert self._redis is not None
        try:
            pubsub = self._redis.pubsub()
            await pubsub.subscribe(TREE_CHANNEL)
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                await self._broadcast_local(message["data"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"WebSocket: écoute Redis interrompue ({exc})")

    async def _broadcast_local(self, payload: str) -> None:
        """Envoie le payload (string JSON) aux connexions concernées de ce process.

        Le payload contient un `tree_id` : seules les connexions de cette room
        (plus celles sans arbre connu, room "") reçoivent l'événement. Si le
        payload n'a pas de tree_id, on diffuse à tout le monde (héritage).
        """
        try:
            tree_id = json.loads(payload).get("tree_id")
        except Exception:
            tree_id = None

        async with self._lock:
            if tree_id:
                targets = list(self._rooms.get(str(tree_id), set())) + list(self._rooms.get("", set()))
            else:
                targets = [ws for conns in self._rooms.values() for ws in conns]
        dead: list[WebSocket] = []
        for ws in targets:
            try:
                # Timeout d'envoi : un client lent ou mort ne doit pas bloquer la
                # diffusion (et donc les endpoints de mutation qui l'attendent),
                # d'autant plus critique avec WEB_CONCURRENCY=1 (un seul worker).
                await asyncio.wait_for(ws.send_text(payload), timeout=5)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    room = self._ws_room.pop(ws, None)
                    if room is not None and room in self._rooms:
                        self._rooms[room].discard(ws)

    async def broadcast(self, event_type: str, data: Optional[dict] = None,
                        origin_user_id: Optional[str] = None,
                        tree_id: Optional[str] = None) -> None:
        """
        Publie un événement de mutation. Diffusé localement immédiatement, et
        publié sur Redis pour les autres instances.

        event_type : "person.created", "person.updated", "person.deleted",
                     "relationship.created", "relationship.deleted", "media.changed"…
        origin_user_id : auteur de la mutation (le client peut s'ignorer lui-même).
        tree_id : arbre concerné — seuls les clients de cette room sont notifiés.
        """
        payload = json.dumps({
            "type": event_type,
            "data": data or {},
            "origin": origin_user_id,
            "tree_id": tree_id,
        })
        # Si Redis est disponible, on publie UNIQUEMENT sur le canal : la boucle
        # d'écoute (_listen_redis) se charge de la diffusion locale, ce qui évite
        # une double livraison. Sinon (fail-open), diffusion locale directe.
        if self._redis is not None:
            try:
                await self._redis.publish(TREE_CHANNEL, payload)
                return
            except Exception as exc:
                logger.debug(f"WebSocket: publish Redis échoué, repli local ({exc})")
        await self._broadcast_local(payload)


# Singleton partagé par l'application.
manager = ConnectionManager()
