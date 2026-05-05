# Compatible base image for ACA sandbox private DiskId workflows.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY src/agent_swarm_service/orchestration/copilot_runtime.py /opt/agent-swarm/copilot_runtime.py
COPY sandbox-image/run-role.py /opt/agent-swarm/run-role.py

WORKDIR /workspace

RUN apt-get update \
    && apt-get install --no-install-recommends -y git ca-certificates \
    && update-ca-certificates \
    && python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir github-copilot-sdk==0.3.0 \
    && mkdir -p /workspace \
    && chmod 0777 /workspace \
    && rm -rf /var/lib/apt/lists/*

CMD ["/bin/sh"]
