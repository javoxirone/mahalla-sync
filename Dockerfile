FROM python:3.13-slim AS base

COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_CACHE_DIR=/tmp/uv-cache \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project && rm -rf /tmp/uv-cache

COPY manage.py ./
COPY config ./config
COPY mahallas ./mahallas
RUN useradd --create-home --uid 1000 app

FROM base AS development
RUN uv sync --locked --no-install-project \
    && rm -rf /tmp/uv-cache \
    && chown -R app:app /app /opt/venv
USER app
EXPOSE 8000
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]

FROM base AS production
RUN python manage.py collectstatic --noinput
USER app
EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "180", "--graceful-timeout", "180", "--access-logfile", "-"]
