"""
Admin routes — protected by a secret reset token (env var RESET_SECRET).
These endpoints are destructive and must never be exposed without protection.
"""
import os
import logging

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter()

RESET_SECRET = os.getenv("RESET_SECRET", "")


def _check_secret(x_reset_secret: str = Header(default="")):
    if not RESET_SECRET:
        raise HTTPException(status_code=503, detail="RESET_SECRET non configuré sur le serveur")
    if x_reset_secret != RESET_SECRET:
        raise HTTPException(status_code=403, detail="Secret invalide")


@router.post("/reset-db", dependencies=[Depends(_check_secret)])
async def reset_database(db: AsyncSession = Depends(get_db)):
    """
    Supprime toutes les données généalogiques (personnes, relations, médias,
    logs d'audit) mais conserve les comptes utilisateurs.

    Requiert le header X-Reset-Secret = RESET_SECRET (variable d'env).
    """
    await db.execute(text("DELETE FROM audit_logs"))
    await db.execute(text("DELETE FROM relationships"))
    await db.execute(text("DELETE FROM media"))
    await db.execute(text("DELETE FROM canvas_positions"))
    # Détacher les fiches des comptes avant suppression (FK users.person_id → persons.id)
    await db.execute(text("UPDATE users SET person_id = NULL"))
    await db.execute(text("DELETE FROM persons"))
    # Multi-arbre : retirer les accès et les arbres (les comptes restent).
    await db.execute(text("DELETE FROM user_tree_access"))
    await db.execute(text("UPDATE invitations SET family_tree_id = NULL"))
    await db.execute(text("DELETE FROM family_trees"))
    await db.commit()
    logger.warning("reset-db executed: all genealogy data deleted")
    return {"status": "ok", "message": "Données généalogiques supprimées. Comptes utilisateurs conservés."}


# @router.post("/wipe-db", dependencies=[Depends(_check_secret)])
# async def wipe_database(db: AsyncSession = Depends(get_db)):
#     """
#     Supprime TOUTES les données : généalogiques ET comptes utilisateurs.
#     Remet la base dans l'état d'une installation fraîche (tables vides).
#
#     Requiert le header X-Reset-Secret = RESET_SECRET (variable d'env).
#     """
#     await db.execute(text("DELETE FROM audit_logs"))
#     await db.execute(text("DELETE FROM relationships"))
#     await db.execute(text("DELETE FROM media"))
#     await db.execute(text("DELETE FROM canvas_positions"))
#     await db.execute(text("UPDATE users SET person_id = NULL"))
#     await db.execute(text("DELETE FROM persons"))
#     await db.execute(text("DELETE FROM users"))
#     await db.execute(text("DELETE FROM invitations"))
#     await db.commit()
#     logger.warning("wipe-db executed: ALL data deleted including user accounts")
#     return {"status": "ok", "message": "Base complètement vidée. Tous les comptes et données généalogiques supprimés."}
