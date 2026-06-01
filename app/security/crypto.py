"""
Chiffrement applicatif des champs sensibles + hachage déterministe du téléphone.

- Chiffrement : Fernet (AES-128-CBC + HMAC) via MultiFernet pour permettre la
  rotation de clés. Le ciphertext inclut un IV aléatoire → deux chiffrements du
  même clair diffèrent (non déterministe). C'est pourquoi le téléphone, qui doit
  rester recherchable par égalité, utilise EN PLUS un hash déterministe.

- Lecture tolérante : decrypt() retourne la valeur telle quelle si elle n'est pas
  un token Fernet valide (donnée héritée en clair, ou clé non configurée). Cela
  rend la migration progressive et idempotente, et évite tout crash pendant la
  transition.
"""

import hmac
import hashlib
import logging
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.config import settings

logger = logging.getLogger(__name__)


def _build_fernet() -> Optional[MultiFernet]:
    keys = [k.strip() for k in settings.ENCRYPTION_KEYS.split(",") if k.strip()]
    if not keys:
        logger.warning("ENCRYPTION_KEYS non configuré : chiffrement désactivé (no-op).")
        return None
    try:
        return MultiFernet([Fernet(k) for k in keys])
    except Exception as e:  # noqa: BLE001
        logger.error(f"Clés de chiffrement invalides, chiffrement désactivé: {e}")
        return None


_fernet = _build_fernet()


def encryption_enabled() -> bool:
    return _fernet is not None


def encrypt(plaintext: Optional[str]) -> Optional[str]:
    if plaintext is None:
        return None
    if _fernet is None:
        return plaintext
    return _fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: Optional[str]) -> Optional[str]:
    if token is None:
        return None
    if _fernet is None:
        return token
    try:
        return _fernet.decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        # Valeur héritée en clair (avant migration) ou non déchiffrable : on la
        # retourne telle quelle pour ne pas casser la lecture.
        return token


def phone_hash(phone: str) -> str:
    """
    Hash déterministe (HMAC-SHA256) du téléphone normalisé, pour les recherches
    par égalité sans déchiffrer. Stable tant que PHONE_HMAC_KEY ne change pas.
    """
    normalized = phone.strip()
    return hmac.new(
        settings.PHONE_HMAC_KEY.encode("utf-8"),
        normalized.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
