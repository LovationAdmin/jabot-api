"""
Endpoint WebSocket pour la synchronisation temps réel de l'arbre.

Le client se connecte sur /ws/tree?token=<JWT>. À chaque mutation (publiée par
les routes persons/tree/media), il reçoit un message JSON :
    { "type": "person.updated", "data": {...}, "origin": "<user_id>" }
et peut recharger l'arbre. Le champ `origin` permet au client d'ignorer ses
propres mutations s'il le souhaite (il a déjà l'état à jour localement).
"""
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

from app.services.auth_service import decode_token
from app.services.ws_manager import manager

logger = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/tree")
async def tree_ws(websocket: WebSocket, token: str = Query(default="")):
    """
    Canal de synchronisation de l'arbre. Authentification par JWT en query param
    (les headers Authorization ne sont pas transmis par l'API WebSocket du
    navigateur). Une connexion non authentifiée est refusée.
    """
    payload = decode_token(token) if token else None
    if payload is None or not payload.get("sub"):
        await websocket.close(code=4401)  # 4401 = unauthorized (code applicatif)
        return

    await manager.connect(websocket)
    try:
        while True:
            # On garde la connexion ouverte. Les clients peuvent envoyer un ping
            # applicatif ("ping") ; on répond "pong". Tout autre message est ignoré.
            msg = await websocket.receive_text()
            if msg == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug(f"WebSocket fermé sur erreur: {exc}")
    finally:
        await manager.disconnect(websocket)
