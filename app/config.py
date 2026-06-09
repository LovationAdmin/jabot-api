from pydantic_settings import BaseSettings
from pydantic import field_validator
from typing import Optional


class Settings(BaseSettings):
    # Database
    DATABASE_URL: str = "postgresql+asyncpg://user:password@localhost:5432/jabot_db"

    @field_validator("DATABASE_URL")
    @classmethod
    def _normalize_db_url(cls, v: str) -> str:
        if v.startswith("postgres://"):
            v = "postgresql://" + v[len("postgres://"):]
        if v.startswith("postgresql://"):
            v = "postgresql+asyncpg://" + v[len("postgresql://"):]
        return v

    # Redis
    REDIS_URL: str = "redis://localhost:6379"

    # JWT
    SECRET_KEY: str = "change-me-in-production"
    ALGORITHM: str = "HS256"
    # 30 jours + session glissante (token reemis a chaque /me) => un
    # utilisateur actif n'a jamais a refaire la validation OTP.
    ACCESS_TOKEN_EXPIRE_DAYS: int = 30

    # ── Termii SMS (primary — Africa OTP specialist, Senegal + France, ~$0.03-0.48)
    TERMII_API_KEY: str = ""
    # Sender ID approuvé par Termii pour la route internationale France +
    # Sénégal. Tout autre valeur sera rejetée/filtrée par leur plateforme.
    TERMII_SENDER_ID: str = "Lovation"  # max 11 chars, pre-approved by Termii

    # ── Brevo (Sendinblue) SMS (secondary — cheapest for France/EU, ~€0.045)
    BREVO_API_KEY: str = ""
    BREVO_SENDER: str = "JABOT"  # max 11 chars alphanumeric

    # ── Africa's Talking SMS (kept as tertiary fallback)
    AFRICAS_TALKING_API_KEY: str = ""
    AFRICAS_TALKING_USERNAME: str = "sandbox"
    AFRICAS_TALKING_SENDER_ID: str = "JABOT"

    # ── Vonage SMS (last-resort fallback — laisser vide pour désactiver)
    VONAGE_API_KEY: str = ""
    VONAGE_API_SECRET: str = ""
    VONAGE_BRAND_NAME: str = "JabotAI"

    # SMS dev mode : si True, on n'appelle PAS le fournisseur SMS reel et on
    # expose toujours le code dans la reponse (dev_code).
    SMS_DEV_MODE: bool = False

    # ── Quota SMS par numéro — exigence contractuelle Termii (route
    #    internationale) : maximum 2 envois vers un même numéro par 24 h,
    #    sinon le trafic est classé spam et FACTURÉ. Ne pas augmenter sans
    #    accord écrit de Termii.
    SMS_MAX_PER_PHONE_PER_DAY: int = 2
    # Délai minimum (s) entre deux envois vers le même numéro (0 = désactivé).
    SMS_PHONE_COOLDOWN_SECONDS: int = 60

    # Chiffrement applicatif des champs sensibles (téléphone, dates, ville).
    # ENCRYPTION_KEYS : une ou plusieurs clés Fernet (urlsafe base64, 32 octets)
    # séparées par des virgules. La 1re sert au chiffrement ; les suivantes
    # permettent le déchiffrement lors d'une rotation. Vide => chiffrement
    # désactivé (no-op, utile en dev).
    ENCRYPTION_KEYS: str = ""
    # Clé HMAC pour le hachage déterministe du téléphone (lookup à l'aveugle
    # sans déchiffrer). Doit rester stable et secrète.
    PHONE_HMAC_KEY: str = "change-me-phone-hmac-key"

    # Cloudinary
    CLOUDINARY_CLOUD_NAME: str = ""
    CLOUDINARY_API_KEY: str = ""
    CLOUDINARY_API_SECRET: str = ""

    # Invitations par SMS (lien + code) via la cascade fournisseurs.
    # False par défaut (dev) ; activé en production via render.yaml.
    # Les envois consomment le quota par numéro (2 SMS / 24 h).
    INVITATION_ENABLED: bool = False

    # App
    FRONTEND_URL: str = "http://localhost:3000"
    ENVIRONMENT: str = "development"

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
