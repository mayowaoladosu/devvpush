#!/bin/sh
set -eu

set -- uv run --no-sync uvicorn main:app \
  --host 0.0.0.0 \
  --port 8787 \
  --loop uvloop \
  --workers 1 \
  --no-access-log

if [ -n "${NODE_AGENT_TLS_CERT_FILE:-}" ] || [ -n "${NODE_AGENT_TLS_KEY_FILE:-}" ]; then
  if [ -z "${NODE_AGENT_TLS_CERT_FILE:-}" ] || [ -z "${NODE_AGENT_TLS_KEY_FILE:-}" ]; then
    printf '%s\n' 'Both NODE_AGENT_TLS_CERT_FILE and NODE_AGENT_TLS_KEY_FILE are required.' >&2
    exit 1
  fi
  set -- "$@" \
    --ssl-certfile "$NODE_AGENT_TLS_CERT_FILE" \
    --ssl-keyfile "$NODE_AGENT_TLS_KEY_FILE"
fi

exec "$@"
