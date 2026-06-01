"""
Types SQLAlchemy chiffrés. Le chiffrement/déchiffrement est transparent : le
code applicatif (et la recherche, qui opère sur les objets ORM déjà déchiffrés)
manipule des valeurs en clair, seul le stockage en base est chiffré.

Les colonnes sous-jacentes sont du TEXT (le ciphertext Fernet est plus long que
les données d'origine).
"""

from datetime import date
from typing import Optional

from sqlalchemy.types import TypeDecorator, Text

from app.security.crypto import encrypt, decrypt


class EncryptedString(TypeDecorator):
    """Chaîne chiffrée au repos."""
    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Optional[str], dialect) -> Optional[str]:
        return encrypt(value)

    def process_result_value(self, value: Optional[str], dialect) -> Optional[str]:
        return decrypt(value)


class EncryptedDate(TypeDecorator):
    """Date chiffrée au repos, sérialisée en ISO 8601 avant chiffrement."""
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, date):
            value = value.isoformat()
        return encrypt(str(value))

    def process_result_value(self, value: Optional[str], dialect) -> Optional[date]:
        plain = decrypt(value)
        if not plain:
            return None
        try:
            return date.fromisoformat(plain)
        except ValueError:
            return None
