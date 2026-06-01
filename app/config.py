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

    # Vonage SMS (fournisseur principal - couverture mondiale)
    VONAGE_API_KEY: str = ""
    VONAGE_API_SECRET: str = ""
    VONAGE_BRAND_NAME: str = "JabotAI"

    # Africa's Talking SMS (fournisseur de secours)
    AFRICAS_TALKING_API_KEY: str = ""
    AFRICAS_TALKING_USERNAME: str = "sandbox"
    AFRICAS_TALKING_SENDER_ID: str = "JABOT"

    # SMS dev mode : si True, on n'appelle PAS le fournisseur SMS reel et on
    # expose toujours le code dans la reponse (dev_code). Permet de garder les
    # cles Vonage configurees tout en testant sans envoyer de vrai SMS (utile
    # tant que le compte Vonage est en trial / paiement pending).
    SMS_DEV_MODE: bool = False

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

    # Invitations
    # Mettre à True uniquement quand Vonage OTP est opérationnel.
    INVITATION_ENABLED: bool = False

    # App
    FRONTEND_URL: str = "http://localhost:3000"
    ENVIRONMENT: str = "development"

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
