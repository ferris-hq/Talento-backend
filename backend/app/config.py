from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from environment variables (or ../.env in development)."""

    model_config = SettingsConfigDict(env_file=("../.env", ".env"), extra="ignore")

    environment: str = Field(
        default="development", description="development | staging | production"
    )

    # Supabase
    supabase_url: str = Field(description="https://<project-ref>.supabase.co")
    supabase_jwt_secret: str | None = Field(
        default=None,
        description="Legacy HS256 secret. Leave unset for projects on asymmetric signing keys.",
    )
    supabase_jwt_audience: str = "authenticated"
    database_url: str | None = Field(
        default=None,
        description="Postgres connection string (Supabase pooler, session mode) for the service.",
    )

    # Cloudflare R2
    r2_endpoint: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None
    r2_bucket_raw: str = "talento-raw"
    r2_bucket_media: str = "talento-media"
    r2_bucket_private: str = "talento-private"
    r2_public_base_url: str = "https://media.talentoafrica.com"
    # Purges deleted media from Cloudflare's edge cache (token needs Zone > Cache Purge).
    cloudflare_zone_id: str | None = None
    cloudflare_api_token: str | None = None

    # Background jobs
    redis_url: str = "redis://localhost:6379"

    # Upload limits
    max_library_videos: int = 10
    max_upload_bytes: int = 300 * 1024 * 1024
    max_video_seconds: int = 90

    # HTTP
    cors_origins: list[str] = Field(default_factory=list)

    # Observability
    sentry_dsn: str | None = None

    @property
    def supabase_issuer(self) -> str:
        return f"{self.supabase_url.rstrip('/')}/auth/v1"

    @property
    def supabase_jwks_url(self) -> str:
        return f"{self.supabase_issuer}/.well-known/jwks.json"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
