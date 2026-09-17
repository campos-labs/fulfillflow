FROM python:3.13-slim-trixie@sha256:7ce4b6dfe35e55397b7cda544f8a13f191b7ae28dc5aad71fe664dbc9bc2623f AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

RUN python -m pip install --no-cache-dir "uv==0.12.7"

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-group benchmark --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev --no-group benchmark --no-editable


FROM python:3.13-slim-trixie@sha256:7ce4b6dfe35e55397b7cda544f8a13f191b7ae28dc5aad71fe664dbc9bc2623f AS loadgen-builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/loadgen-venv \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /work

RUN python -m pip install --no-cache-dir "uv==0.12.7"

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --group benchmark --no-install-project


FROM python:3.13-slim-trixie@sha256:7ce4b6dfe35e55397b7cda544f8a13f191b7ae28dc5aad71fe664dbc9bc2623f AS runtime

ENV APP_HOST=0.0.0.0 \
    APP_PORT=8000 \
    PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 fulfillflow \
    && useradd --uid 10001 --gid 10001 --home-dir /app --no-create-home \
        --shell /usr/sbin/nologin fulfillflow

WORKDIR /app

COPY --from=builder --chown=10001:10001 /opt/venv /opt/venv
COPY --chown=10001:10001 alembic.ini ./alembic.ini
COPY --chown=10001:10001 alembic ./alembic
COPY --chown=10001:10001 alembic_core.ini alembic_tracking.ini alembic_notifications.ini ./
COPY --chown=10001:10001 alembic_core ./alembic_core
COPY --chown=10001:10001 alembic_tracking ./alembic_tracking
COPY --chown=10001:10001 alembic_notifications ./alembic_notifications

USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('APP_PORT', '8000')}/health/live\", timeout=2).close()"]

CMD ["python", "-m", "fulfillflow"]


FROM python:3.13-slim-trixie@sha256:7ce4b6dfe35e55397b7cda544f8a13f191b7ae28dc5aad71fe664dbc9bc2623f AS loadgen

ENV PATH=/opt/loadgen-venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10002 loadgen \
    && useradd --uid 10002 --gid 10002 --home-dir /work --no-create-home \
        --shell /usr/sbin/nologin loadgen

WORKDIR /work

COPY --from=loadgen-builder --chown=10002:10002 /opt/loadgen-venv /opt/loadgen-venv
COPY --chown=10002:10002 benchmarks ./benchmarks

USER 10002:10002

ENTRYPOINT ["python", "-m", "locust"]
CMD ["--locustfile", "benchmarks/locustfile.py", "--headless"]


# Keep an unqualified `docker build .` equivalent to the application runtime.
FROM runtime AS final
