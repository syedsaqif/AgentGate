#!/usr/bin/env bash
set -euo pipefail
: "${PRESENTER_PASSWORD:?Missing presenter password}" "${D06_OPS_PASSWORD:?Missing ops password}" "${AGENTGATE_BANK_SERVICE_CREDENTIAL:?Missing bank service credential}"
[[ ${D06_CUSTOMER_PIN:-} =~ ^[0-9]{4}$ ]] || { echo "Set D06_CUSTOMER_PIN to four digits." >&2; exit 1; }
export PORT="${PORT:-10000}"
mkdir -p runtime
printf 'presenter:%s\n' "$(printf '%s\n' "$PRESENTER_PASSWORD" | openssl passwd -apr1 -stdin)" > /etc/nginx/presenter.htpasswd
envsubst '$PORT' < deploy/nginx.conf > /etc/nginx/conf.d/agentgate.conf
rm -f /etc/nginx/sites-enabled/default
nginx -t
python -m tools.seed --profile real
python -m tools.dev stack --profile real &
stack=$!
nginx -g 'daemon off;' &
proxy=$!
trap 'kill "$stack" "$proxy" 2>/dev/null || true' EXIT
trap 'exit 0' TERM INT
wait -n "$stack" "$proxy"
