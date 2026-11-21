# Stage 1: Build React 19 Frontend SPA
FROM node:24-alpine AS frontend-builder
WORKDIR /app

# Copy contracts needed by frontend contract schema tests
COPY contracts/ ./contracts/
COPY frontend/package*.json ./frontend/

WORKDIR /app/frontend
RUN npm ci

COPY frontend/ ./
RUN npm run build

# Stage 2: Python 3.12 Backend & Production Nginx Runner
FROM python:3.12-slim
WORKDIR /app

# Install system dependencies: Nginx, curl, gettext (for envsubst)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    nginx \
    gettext-base \
    && rm -rf /var/lib/apt/lists/*

# Install uv package manager
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /bin/uv

# Copy Python dependency specifications and install
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy application source code
COPY contracts/ ./contracts/
COPY gateway/ ./gateway/
COPY bank_mock/ ./bank_mock/
COPY demo_server/ ./demo_server/
COPY agent_sdk/ ./agent_sdk/
COPY simulator/ ./simulator/
COPY tools/ ./tools/

# Create runtime directory for dynamic databases and cryptographic keys
RUN mkdir -p /app/runtime

# Copy built frontend distribution from Stage 1
COPY --from=frontend-builder /app/frontend/dist /app/frontend/dist

# Copy entrypoint and Nginx configuration template
COPY nginx.conf.template /app/nginx.conf.template
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app"
ENV AGENTGATE_PROFILE="real"
ENV AGENTGATE_GATEWAY_DB="/app/runtime/gateway.db"
ENV AGENTGATE_BANK_DB="/app/runtime/bank.db"

EXPOSE 10000

ENTRYPOINT ["/app/entrypoint.sh"]
