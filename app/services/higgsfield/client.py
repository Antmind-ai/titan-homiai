from __future__ import annotations

import asyncio
import json
from typing import Any

from loguru import logger

from app.core.config import settings


GENERATE_TIMEOUT = settings.higgsfield_timeout_minutes * 60


class HiggsfieldError(Exception):
    pass


class HiggsfieldGenerateResult:
    def __init__(self, url: str, media_type: str, job_id: str | None = None) -> None:
        self.url = url
        self.media_type = media_type  # "image" or "video"
        self.job_id = job_id


def _is_retryable_higgsfield_error(message: str) -> bool:
    normalized = message.lower()
    retryable_markers = (
        "http 500",
        "http 502",
        "http 503",
        "http 504",
        "internal server error",
        "timed out",
        "timeout",
        "temporarily unavailable",
        "connection reset",
    )
    return any(marker in normalized for marker in retryable_markers)


async def _run_higgsfield(
    *args: str,
    timeout: int = GENERATE_TIMEOUT,
) -> tuple[int, str, str]:
    cmd = [settings.higgsfield_bin, *args]
    logger.info("Higgsfield CLI | spawning | cmd={}", " ".join(cmd))

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise HiggsfieldError(
            f"Higgsfield CLI timed out after {timeout}s"
        )

    stdout_str = stdout.decode("utf-8", errors="replace").strip()
    stderr_str = stderr.decode("utf-8", errors="replace").strip()

    logger.info(
        "Higgsfield CLI | exit={} | stdout_len={} | stderr_len={}",
        process.returncode,
        len(stdout_str),
        len(stderr_str),
    )

    return process.returncode or 0, stdout_str, stderr_str


def _extract_url(job: dict[str, Any]) -> HiggsfieldGenerateResult | None:
    job_id = job.get("id")

    # Primary: result_url at top level (actual HF API shape)
    url = job.get("result_url")
    if url and isinstance(url, str):
        return HiggsfieldGenerateResult(url=url, media_type="image", job_id=job_id)

    # Fallback: result.media (array)
    result = job.get("result")
    if isinstance(result, dict):
        media = result.get("media")
        if isinstance(media, list) and media:
            item = media[0]
            url = item.get("url")
            if url:
                return HiggsfieldGenerateResult(
                    url=url,
                    media_type=item.get("type", "image"),
                    job_id=job_id,
                )
        if isinstance(media, dict):
            url = media.get("url")
            if url:
                return HiggsfieldGenerateResult(
                    url=url,
                    media_type=media.get("type", "image"),
                    job_id=job_id,
                )
        url = result.get("url")
        if url:
            return HiggsfieldGenerateResult(url=url, media_type="image", job_id=job_id)

    # Fallback: top-level media / medias
    for key in ("media", "medias"):
        val = job.get(key)
        if isinstance(val, list) and val:
            item = val[0]
            if isinstance(item, dict):
                url = item.get("url")
                if url:
                    return HiggsfieldGenerateResult(
                        url=url,
                        media_type=item.get("type", "image"),
                        job_id=job_id,
                    )
        if isinstance(val, dict):
            url = val.get("url")
            if url:
                return HiggsfieldGenerateResult(
                    url=url,
                    media_type=val.get("type", "image"),
                    job_id=job_id,
                )

    return None


def _parse_result(output: str) -> HiggsfieldGenerateResult:
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        raise HiggsfieldError(f"Failed to parse Higgsfield JSON output: {output[:500]}")

    # Handle single job object
    if isinstance(data, dict):
        result = _extract_url(data)
        if result:
            return result
        raise HiggsfieldError(
            f"Could not extract media URL | keys={list(data.keys())} | status={data.get('status')}"
        )

    # Handle array of job objects
    if isinstance(data, list):
        if not data:
            raise HiggsfieldError("Higgsfield returned empty result array")

        for job in reversed(data):
            if not isinstance(job, dict):
                continue
            result = _extract_url(job)
            if result:
                return result

        last = data[-1] if isinstance(data[-1], dict) else {}
        raise HiggsfieldError(
            f"Could not extract media URL from any job | last_keys={list(last.keys())} | status={last.get('status')}"
        )

    raise HiggsfieldError(f"Unexpected Higgsfield response type: {type(data).__name__}")


async def generate_image(
    *,
    model: str,
    prompt: str,
    image_path: str,
    quality: str = "high",
    aspect_ratio: str = "1:1",
    timeout: int = GENERATE_TIMEOUT,
) -> HiggsfieldGenerateResult:
    args = [
        "generate",
        "create",
        model,
        "--prompt", prompt,
        "--image", image_path,
        "--quality", quality,
        "--aspect_ratio", aspect_ratio,
        "--wait",
        "--json",
    ]

    max_attempts = 3
    backoff_seconds = (1.5, 3.0)
    last_error: HiggsfieldError | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            returncode, stdout, stderr = await _run_higgsfield(*args, timeout=timeout)

            if returncode != 0:
                raise HiggsfieldError(
                    f"Higgsfield CLI exited with code {returncode} | stderr={stderr[:500]}"
                )

            if stderr and "error" in stderr.lower():
                raise HiggsfieldError(f"Higgsfield CLI reported error: {stderr[:500]}")

            return _parse_result(stdout)
        except HiggsfieldError as exc:
            last_error = exc
            retryable = _is_retryable_higgsfield_error(str(exc))
            if attempt >= max_attempts or not retryable:
                raise

            delay = backoff_seconds[min(attempt - 1, len(backoff_seconds) - 1)]
            logger.warning(
                "Higgsfield call failed (retrying) | attempt={}/{} | delay={}s | error={}",
                attempt,
                max_attempts,
                delay,
                str(exc)[:300],
            )
            await asyncio.sleep(delay)

    if last_error is not None:
        raise last_error
    raise HiggsfieldError("Higgsfield generation failed with unknown error")
