from __future__ import annotations

from dataclasses import dataclass


class DesignGenerationError(Exception):
    """Raised when image generation fails across all configured providers."""


@dataclass(slots=True)
class DesignGenerationResult:
    url: str
    media_type: str = "image"
    job_id: str | None = None
