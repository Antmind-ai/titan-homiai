from __future__ import annotations

from loguru import logger

from app.core.config import settings
from app.services.design_generation import fal as fal_provider
from app.services.design_generation.models import (
    DesignGenerationError,
    DesignGenerationResult,
)
from app.services.higgsfield import client as higgsfield_provider

# Model ID → credits consumed per image
MODEL_CREDIT_COST: dict[str, int] = {
    "fal-ai/bytedance/seedream/v4.5/edit": 25,
    "fal-ai/nano-banana-pro/edit": 75,
    # Higgsfield model IDs
    "seedream_v4_5": 25,
}

# Model ID → USD cost per image (fal.ai pricing)
MODEL_API_COST: dict[str, float] = {
    "fal-ai/bytedance/seedream/v4.5/edit": 0.04,
    "fal-ai/nano-banana-pro/edit": 0.15,
    # Nano Banana Pro 4K outputs cost $0.30
    # Higgsfield model IDs
    "seedream_v4_5": 0.04,
}


def get_model_credit_cost(model: str) -> int:
    return MODEL_CREDIT_COST.get(model, 25)


def get_model_api_cost(model: str) -> float:
    return MODEL_API_COST.get(model, 0.04)


def _higgsfield_enabled() -> bool:
    return settings.enable_higgsfield_backend


async def _generate_higgsfield(
    *,
    prompt: str,
    image_path: str,
) -> DesignGenerationResult:
    if not _higgsfield_enabled():
        raise DesignGenerationError(
            "Higgsfield backend is disabled. Set ENABLE_HIGGSFIELD_BACKEND=true to enable it."
        )

    try:
        result = await higgsfield_provider.generate_image(
            model=settings.higgsfield_design_model,
            prompt=prompt,
            image_path=image_path,
            quality=settings.higgsfield_design_quality,
            aspect_ratio=settings.higgsfield_design_aspect_ratio,
        )
    except higgsfield_provider.HiggsfieldError:
        raise
    except Exception as exc:
        raise DesignGenerationError(
            f"Higgsfield generation failed unexpectedly: {exc}"
        ) from exc
    return DesignGenerationResult(
        url=result.url,
        media_type=result.media_type,
        job_id=result.job_id,
        model=settings.higgsfield_design_model,
    )


async def _generate_fal(
    *,
    prompt: str,
    image_path: str,
) -> DesignGenerationResult:
    errors: list[str] = []
    candidates = settings.fal_design_model_candidates

    for model in candidates:
        try:
            if len(candidates) > 1:
                logger.info("fal.ai model attempt | model={}", model)

            result = await fal_provider.generate_image(
                model=model,
                prompt=prompt,
                image_path=image_path,
                aspect_ratio=settings.fal_design_aspect_ratio,
                resolution=settings.fal_design_resolution,
                output_format=settings.fal_design_output_format,
                timeout=settings.fal_timeout_minutes * 60,
            )
            return result
        except Exception as exc:
            error_msg = str(exc)[:500]
            errors.append(f"{model}: {error_msg}")
            if len(candidates) > 1:
                logger.warning(
                    "fal.ai model attempt failed | model={} | error={}",
                    model,
                    error_msg,
                )

    raise fal_provider.FalGenerationError(
        "fal.ai generation failed for all configured models | "
        + " ; ".join(errors)
    )


async def generate_image(
    *,
    prompt: str,
    image_path: str,
) -> DesignGenerationResult:
    provider = settings.design_generation_provider

    if provider == "higgsfield":
        return await _generate_higgsfield(prompt=prompt, image_path=image_path)

    if provider != "fal":
        raise DesignGenerationError(
            f"Unsupported DESIGN_GENERATION_PROVIDER value: {provider}"
        )

    try:
        return await _generate_fal(prompt=prompt, image_path=image_path)
    except Exception as exc:
        if isinstance(exc, fal_provider.FalGenerationError):
            fal_error = exc
        else:
            fal_error = fal_provider.FalGenerationError(
                f"fal.ai generation failed unexpectedly: {exc}"
            )

        if not _higgsfield_enabled():
            if isinstance(exc, fal_provider.FalGenerationError):
                raise
            raise fal_error from exc

        logger.warning(
            "fal.ai generation failed; attempting Higgsfield fallback | error={}",
            str(fal_error)[:500],
        )

        try:
            return await _generate_higgsfield(prompt=prompt, image_path=image_path)
        except DesignGenerationError as hf_error:
            raise DesignGenerationError(
                f"fal.ai failed: {fal_error}; Higgsfield fallback failed: {hf_error}"
            ) from hf_error
