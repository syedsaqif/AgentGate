FROM node:24-bookworm-slim AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
ENV VITE_GATEWAY_ORIGIN=/ VITE_DEMO_AGENT_ORIGIN=/ VITE_DEMO_BANK_ORIGIN=/ VITE_DEMO_GATE_STREAM_URL=/demo/gate/stream
RUN npm run build

FROM python:3.12-slim-bookworm
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends nginx openssl gettext-base bash && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
COPY config.yaml ./
COPY agent_sdk/ agent_sdk/
COPY bank_mock/ bank_mock/
COPY contracts/ contracts/
COPY demo_server/ demo_server/
COPY gateway/ gateway/
COPY simulator/ simulator/
COPY tools/ tools/
COPY deploy/ deploy/
COPY --from=frontend /build/dist/ /srv/agentgate/
CMD ["bash", "deploy/start.sh"]
