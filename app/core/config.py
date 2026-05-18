from functools import lru_cache
from typing import Literal
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
    design_upload_dir: str = "/tmp/titan/design-inputs"  # noqa: S108
    design_upload_max_mb: int = Field(default=15, ge=1, le=50)
    design_output_dir: str = "/tmp/titan/design-outputs"  # noqa: S108
    design_generation_provider: Literal["fal", "higgsfield"] = "fal"
    enable_higgsfield_backend: bool = False
    fal_key: str | None = Field(default=None)
    fal_timeout_minutes: int = Field(default=20, ge=1, le=120)
    fal_timeout_ms: int = Field(default=900000, ge=1000, le=7200000)
    fal_design_model: str = "fal-ai/bytedance/seedream/v4.5/edit"
    fal_design_aspect_ratio: str = "1:1"
    fal_design_resolution: Literal["1K", "2K", "4K"] = "1K"
    fal_design_output_format: Literal["jpeg", "png", "webp"] = "png"
    fal_segmentation_model_id: str = "fal-ai/sam-3-1/image"
    fal_fill_model_id: str = "fal-ai/flux-pro/v1/fill"
    higgsfield_timeout_minutes: int = Field(default=20, ge=1, le=120)
    higgsfield_bin: str = "higgsfield"
    higgsfield_design_model: str = "seedream_v4_5"
    higgsfield_design_quality: str = "high"
    higgsfield_design_aspect_ratio: str = "1:1"
    app_environment: str = "development"
    free_lifetime_credits: int = Field(default=25, ge=0, le=1000)
    credits_internal_api_key: str | None = Field(default=None)
    enable_credit_self_topup: bool = False
    credit_self_topup_amount: int = Field(default=75, ge=1, le=1000)
    api_v1_prefix: str = "/api/v1"
    log_level: str = "INFO"

    # ── Cloudflare R2 ──────────────────────────────────────────────────────────
    r2_endpoint_url: str | None = Field(default=None)
    r2_bucket_name: str | None = Field(default=None)
    r2_access_key_id: str | None = Field(default=None)
    r2_secret_access_key: str | None = Field(default=None)
    r2_public_url: str | None = Field(default=None)
    r2_presigned_url_expiry: int = Field(default=3600, ge=60, le=86400)
    r2_download_url_expiry: int = Field(default=3600, ge=60, le=86400)
    r2_region: str = "auto"
    s3_force_path_style: bool = True

    # ── RevenueCat ─────────────────────────────────────────────────────────────
    revenuecat_api_key: str | None = Field(default=None)
    revenuecat_webhook_secret: str | None = Field(default=None)
    # NOTE: configure these env vars with real App Store / Play product SKUs in deployed envs.
    # The fallback values below are only safe for local development placeholders.
    subscription_weekly_product_id: str = "weekly"
    subscription_yearly_product_id: str = "yearly"
    subscription_weekly_credits: int = Field(default=350, ge=1, le=10000)
    subscription_yearly_credits: int = Field(default=2000, ge=1, le=100000)

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
            return (
                f"redis://:{encoded_password}@{self.redis_host}:{self.redis_port}/{self.redis_db}"
            )
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @computed_field
    @property
    def design_upload_max_bytes(self) -> int:
        return self.design_upload_max_mb * 1024 * 1024

    @computed_field
    @property
    def fal_design_model_candidates(self) -> list[str]:
        candidates: list[str] = []
        seen: set[str] = set()

        for raw_model in self.fal_design_model.split(","):
            model = raw_model.strip()
            if not model or model in seen:
                continue
            candidates.append(model)
            seen.add(model)

        if not candidates:
            raise ValueError(
                "FAL_DESIGN_MODEL must contain at least one model id "
                "(comma-separated is supported)."
            )

        return candidates


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
