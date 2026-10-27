#!/usr/bin/env bash
# Demo setup through the admin API: register + approve + deploy the Ollama model, create a
# tenant and print its API key (shown once). Safe to re-run: existing objects are reused.
set -euo pipefail

GATEWAY_URL="${GATEWAY_URL:-http://localhost:${GATEWAY_PORT:-8000}}"
if [[ -z "${GATEWAY_ADMIN_API_KEY:-}" && -f .env ]]; then
  GATEWAY_ADMIN_API_KEY="$(grep -E '^GATEWAY_ADMIN_API_KEY=' .env | cut -d= -f2-)"
fi
: "${GATEWAY_ADMIN_API_KEY:?set GATEWAY_ADMIN_API_KEY or create .env}"
MODEL_NAME="${MODEL_NAME:-qwen-small}"
UPSTREAM="${OLLAMA_MODEL:-qwen2.5:0.5b}"
TENANT="${TENANT:-demo}"

api() { # api METHOD PATH [JSON]  -> prints body, never fails the script on 4xx
  curl -sS -X "$1" "$GATEWAY_URL$2" \
    -H "Authorization: Bearer $GATEWAY_ADMIN_API_KEY" \
    -H "Content-Type: application/json" ${3:+-d "$3"}
}

echo "Waiting for $GATEWAY_URL/readyz ..."
until curl -sf "$GATEWAY_URL/readyz" >/dev/null; do sleep 2; done

api POST /admin/models \
  "{\"name\":\"$MODEL_NAME\",\"backend\":\"ollama\",\"upstream_model\":\"$UPSTREAM\",\"owner\":\"platform\"}" >/dev/null
api POST "/admin/models/$MODEL_NAME/approve" >/dev/null
api POST "/admin/models/$MODEL_NAME/deploy" >/dev/null
echo "Model '$MODEL_NAME' -> ollama:$UPSTREAM is deployed."

api POST /admin/tenants "{\"name\":\"$TENANT\"}" >/dev/null
TENANT_ID="$(api GET /admin/tenants | python3 -c "
import json, sys
print(next(t['id'] for t in json.load(sys.stdin) if t['name'] == '$TENANT'))")"
KEY="$(api POST "/admin/tenants/$TENANT_ID/keys" | python3 -c "import json,sys; print(json.load(sys.stdin)['key'])")"

echo "Tenant '$TENANT' (id $TENANT_ID) API key (shown once):"
echo "  export OPENAI_API_KEY=$KEY"
echo "  export OPENAI_BASE_URL=$GATEWAY_URL/v1"
