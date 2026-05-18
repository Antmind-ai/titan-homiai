from arq.connections import RedisSettings
from loguru import logger

from app.core.config import settings
from app.workers.tasks import (
    cleanup_user_data_task,
    health_ping_task,
    process_design_request_task,
    process_object_replace_request_task,
)


async def startup(ctx: dict) -> None:
    logger.info("ARQ worker startup | queue={}", settings.arq_queue_name)


async def shutdown(ctx: dict) -> None:
    logger.info("ARQ worker shutdown")


class WorkerSettings:
    functions = (
        health_ping_task,
        process_design_request_task,
        process_object_replace_request_task,
        cleanup_user_data_task,
    )
    redis_settings = RedisSettings(
        host=settings.redis_host,
        port=settings.redis_port,
        password=settings.redis_password,
        database=settings.redis_db,
    )
    queue_name = settings.arq_queue_name
    max_jobs = settings.arq_max_jobs
    job_timeout = settings.arq_job_timeout_seconds
    keep_result = settings.arq_keep_result_seconds
    on_startup = startup
    on_shutdown = shutdown
