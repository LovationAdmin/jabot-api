"""Encrypt sensitive fields at rest: users.phone (+phone_hash), persons.birth_date / death_date / city_of_origin.

Revision ID: 005
Revises: 004
Create Date: 2026-06-01 00:00:00.000000

Stratégie :
1. Élargir les colonnes en TEXT (le ciphertext Fernet est plus long que l'origine).
2. Ajouter users.phone_hash (hash déterministe pour le lookup).
3. Backfill en Python : hacher + chiffrer les valeurs existantes.
4. Imposer NOT NULL / unicité sur phone_hash, retirer l'unicité sur phone clair.

Idempotence / sécurité :
- Si ENCRYPTION_KEYS n'est pas configuré, encrypt() est un no-op : la migration
  se contente de poser phone_hash et d'élargir les colonnes (données en clair).
  Le chiffrement réel s'activera dès que les clés seront présentes.
- decrypt() étant tolérant au clair, l'app fonctionne quel que soit l'état.

⚠️ Déploiement : définir ENCRYPTION_KEYS et PHONE_HMAC_KEY AVANT de migrer, et
sauvegarder la base au préalable.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.security.crypto import encrypt, phone_hash

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── 1. Élargir les colonnes pour accueillir le ciphertext ──────────────
    # persons : Date → Text (sérialisation ISO chiffrée), city String(200) → Text
    op.alter_column(
        "persons", "birth_date",
        existing_type=sa.Date(), type_=sa.Text(),
        postgresql_using="birth_date::text", existing_nullable=True,
    )
    op.alter_column(
        "persons", "death_date",
        existing_type=sa.Date(), type_=sa.Text(),
        postgresql_using="death_date::text", existing_nullable=True,
    )
    op.alter_column(
        "persons", "city_of_origin",
        existing_type=sa.String(length=200), type_=sa.Text(),
        existing_nullable=True,
    )
    # users.phone String(20) → Text
    op.alter_column(
        "users", "phone",
        existing_type=sa.String(length=20), type_=sa.Text(),
        existing_nullable=False,
    )

    # ── 2. Ajouter phone_hash (nullable le temps du backfill) ──────────────
    op.add_column("users", sa.Column("phone_hash", sa.String(length=64), nullable=True))

    # ── 3. Backfill : hacher + chiffrer les valeurs existantes ─────────────
    users = conn.execute(sa.text("SELECT id, phone FROM users")).fetchall()
    for row in users:
        uid, phone = row[0], row[1]
        if phone is None:
            continue
        conn.execute(
            sa.text("UPDATE users SET phone = :p, phone_hash = :h WHERE id = :id"),
            {"p": encrypt(phone), "h": phone_hash(phone), "id": uid},
        )

    persons = conn.execute(
        sa.text("SELECT id, birth_date, death_date, city_of_origin FROM persons")
    ).fetchall()
    for row in persons:
        pid, bd, dd, city = row[0], row[1], row[2], row[3]
        conn.execute(
            sa.text(
                "UPDATE persons SET birth_date = :bd, death_date = :dd, "
                "city_of_origin = :city WHERE id = :id"
            ),
            {
                "bd": encrypt(bd) if bd is not None else None,
                "dd": encrypt(dd) if dd is not None else None,
                "city": encrypt(city) if city is not None else None,
                "id": pid,
            },
        )

    # ── 4. Contraintes : phone_hash NOT NULL + unique, phone non unique ────
    op.alter_column("users", "phone_hash", existing_type=sa.String(length=64), nullable=False)
    op.create_index("ix_users_phone_hash", "users", ["phone_hash"], unique=True)
    # L'unicité portait sur le téléphone clair ; elle n'a plus de sens sur un
    # ciphertext non déterministe. Drop best-effort (nom par défaut PostgreSQL).
    try:
        op.drop_constraint("users_phone_key", "users", type_="unique")
    except Exception:
        pass


def downgrade() -> None:
    # Retour arrière best-effort. Les valeurs restent chiffrées (le clair n'est
    # pas restauré) : à n'utiliser qu'avec une restauration de sauvegarde.
    op.drop_index("ix_users_phone_hash", table_name="users")
    op.drop_column("users", "phone_hash")
    op.alter_column("users", "phone", existing_type=sa.Text(), type_=sa.String(length=20), existing_nullable=False)
    op.alter_column("persons", "city_of_origin", existing_type=sa.Text(), type_=sa.String(length=200), existing_nullable=True)
    op.alter_column("persons", "death_date", existing_type=sa.Text(), type_=sa.Date(), postgresql_using="death_date::date", existing_nullable=True)
    op.alter_column("persons", "birth_date", existing_type=sa.Text(), type_=sa.Date(), postgresql_using="birth_date::date", existing_nullable=True)
