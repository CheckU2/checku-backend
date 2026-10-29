from functools import lru_cache
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_name: str = "CheckU API"
    app_env: str = "development"
    api_v1_prefix: str = "/api/v1"
    host: str = "127.0.0.1"
    port: int = 8000
    mongodb_uri: str = ""
    mongodb_database: str = "checku"
    jwt_secret_key: str = ""
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 10
    refresh_token_expire_days: int = 30
    password_reset_expire_minutes: int = 30
    institutional_timezone: str = "America/Mexico_City"
    cors_origins: str = "http://localhost:5173,http://localhost:8081,http://localhost:19006"
    bootstrap_admin_email: str = ""
    bootstrap_admin_password: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_use_tls: bool = True
    public_registration_enabled: bool = True
    qr_challenge_ttl_seconds: int = 60
    password_reset_base_url: str = "http://localhost:5173/reset-password"
    upload_dir: str = "private_uploads"
    max_avatar_bytes: int = 4 * 1024 * 1024
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore")
    @property
    def cors_origin_list(self) -> list[str]:
        return [x.strip() for x in self.cors_origins.split(",") if x.strip()]
    @model_validator(mode="after")
    def validate_security(self):
        if self.app_env.lower() == "production" and len(self.jwt_secret_key) < 32:
            raise ValueError("JWT_SECRET_KEY debe tener al menos 32 caracteres en producción")
        if not 1 <= self.access_token_expire_minutes <= 60:
            raise ValueError("ACCESS_TOKEN_EXPIRE_MINUTES debe estar entre 1 y 60")
        if self.institutional_timezone != "America/Mexico_City":
            # Other IANA zones are permitted if institutional policy changes.
            from zoneinfo import ZoneInfo
            ZoneInfo(self.institutional_timezone)
        return self

@lru_cache
def get_settings() -> Settings:
    return Settings()
