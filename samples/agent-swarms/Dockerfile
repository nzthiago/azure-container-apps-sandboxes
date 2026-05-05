FROM python:3.12-slim AS app-wheel-builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

RUN python -m pip install --no-cache-dir --upgrade pip

COPY pyproject.toml ./
COPY src ./src
COPY vendor/wheels /vendor-wheels

RUN python -m pip wheel --no-cache-dir --no-deps --wheel-dir /wheels --find-links=/vendor-wheels . \
    && python -m pip download --no-cache-dir --dest /wheels --find-links=/vendor-wheels \
    /wheels/agent_swarm_service-*.whl \
    azure-containerapps-sandbox

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends -y git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir --upgrade pip

COPY --from=app-wheel-builder /wheels /wheels

RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels \
    agent-swarm-service \
    azure-containerapps-sandbox \
    && rm -rf /wheels

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "agent_swarm_service.app:create_runtime_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
