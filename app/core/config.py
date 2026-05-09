from functools import lru_cache
from urllib.parse import quote

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App ───────────────────────────────────────────────────────────────────
    app_name: str = "Titan"
    app_version: str = "1.0.0"
    secret_key: str = Field(..., min_length=32)
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = Field(default=60 * 24 * 30, ge=1)
    design_upload_dir: str = "/tmp/titan/design-inputs"
    design_upload_max_mb: int = Field(default=15, ge=1, le=50)
    app_environment: str = "development"
    free_lifetime_credits: int = Field(default=3, ge=0, le=1000)
    credits_internal_api_key: str | None = Field(default=None)
    enable_credit_self_topup: bool = False
    credit_self_topup_amount: int = Field(default=3, ge=1, le=1000)
    api_v1_prefix: str = "/api/v1"
    log_level: str = "INFO"

    # ── Database ──────────────────────────────────────────────────────────────
    db_host: str = "postgres"
    db_port: int = 5432
    db_user: str = "titan"
    db_password: str = Field(...)
    db_name: str = "titan"
    db_pool_size: int = 20
    db_max_overflow: int = 40
    db_pool_timeout: int = 30
    db_pool_recycle: int = 1800
    db_echo: bool = False

    # ── Redis ─────────────────────────────────────────────────────────────────
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_password: str = Field(...)
    redis_db: int = 0
    redis_max_connections: int = 100

    # ── ARQ ───────────────────────────────────────────────────────────────────
    arq_queue_name: str = "titan:queue"
    arq_max_jobs: int = 100
    arq_job_timeout_seconds: int = 300
    arq_keep_result_seconds: int = 3600

    @computed_field
    @property
    def database_url(self) -> str:
        return URL.create(
            drivername="postgresql+asyncpg",
            username=self.db_user,
            password=self.db_password,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        ).render_as_string(hide_password=False)

    @computed_field
    @property
    def database_url_sync(self) -> str:
        return URL.create(
            drivername="postgresql+psycopg2",
            username=self.db_user,
            password=self.db_password,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        ).render_as_string(hide_password=False)

    @computed_field
    @property
    def redis_url(self) -> str:
        if self.redis_password:
            encoded_password = quote(self.redis_password, safe="")
            return f"redis://:{encoded_password}@{self.redis_host}:{self.redis_port}/{self.redis_db}"
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @computed_field
    @property
    def design_upload_max_bytes(self) -> int:
        return self.design_upload_max_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
