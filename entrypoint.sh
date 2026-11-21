#!/bin/bash
set -e

export PORT="${PORT:-10000}"
echo "================================================="
echo "Starting AgentGate on port $PORT..."
echo "================================================="

# Substitute $PORT into nginx configuration
envsubst '$PORT' < /app/nginx.conf.template > /etc/nginx/nginx.conf

# Start backend services stack in background
uv run python -m tools.dev stack --profile real &

# Wait for gateway service to become ready on port 8000
echo "Waiting for Gateway service to initialize..."
for i in $(seq 1 45); do
    if curl -s http://127.0.0.1:8000/health/ready > /dev/null 2>&1; then
        echo "Gateway service is healthy and ready!"
        break
    fi
    sleep 1
done

# Start Nginx in foreground on $PORT
echo "Starting Nginx frontend & reverse-proxy on port $PORT..."
exec nginx -g 'daemon off;'
