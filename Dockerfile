# ── Builder stage ─────────────────────────────────────────────────────────────
FROM python:3.13-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_PROJECT_ENVIRONMENT=/venv PATH=/venv/bin:$PATH

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-editable


# ── Production stage ──────────────────────────────────────────────────────────
FROM python:3.13-slim AS production

LABEL maintainer="Antmind Ventures Private Limited"
LABEL org.opencontainers.image.title="Titan API"
LABEL org.opencontainers.image.version="1.0.0"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://raw.githubusercontent.com/higgsfield-ai/cli/main/install.sh | sh \
    && groupadd -r titan \
    && useradd -r -g titan -d /app -s /sbin/nologin titan

COPY --from=builder /venv /venv
ENV PATH=/venv/bin:$PATH \
    HOME=/app \
    XDG_CONFIG_HOME=/app/.config \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1

COPY --chown=titan:titan . .
RUN chmod +x docker/entrypoint.sh \
    && mkdir -p /app/.config \
    && chown titan:titan /app/.config

USER titan

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/api/v1/health || exit 1

ENTRYPOINT ["docker/entrypoint.sh"]
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app.main:app"]
