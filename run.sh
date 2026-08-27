#!/usr/bin/env bash
# Ubuntu / WSL / Linux launcher for cline-reason-proxy
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 missing. Ubuntu: sudo apt update && sudo apt install -y python3"
  exit 1
fi

if [ ! -f config.json ]; then
  cp config.example.json config.json
  python3 - <<'PY'
import json, secrets
from pathlib import Path
p = Path("config.json")
cfg = json.loads(p.read_text())
if not cfg.get("api_key") or cfg["api_key"] in ("sk-crp-change-me", ""):
    cfg["api_key"] = "sk-crp-" + secrets.token_urlsafe(24)
cfg.setdefault("host", "0.0.0.0")
cfg.setdefault("port", 20129)
cfg.setdefault("reasoning_effort", "max")
p.write_text(json.dumps(cfg, indent=2) + "\n")
print("wrote config.json (proxy API key generated)")
PY
fi

if [ ! -f keys.json ]; then
  if [ -n "${CLINE_API_KEYS:-}${CLINE_API_KEY:-}" ]; then
    :
  elif [ -f "${HOME}/.9router/db/data.sqlite" ]; then
    echo "using 9router db at ~/.9router/db/data.sqlite"
  else
    echo "No Cline keys."
    echo "  1) copy keys.example.json -> keys.json and put sk_ keys, or"
    echo "  2) export CLINE_API_KEYS=sk_...,sk_..., or"
    echo "  3) point NINE_ROUTER_DB at 9router sqlite"
    exit 1
  fi
fi

export CLINE_PROXY_HOST="${CLINE_PROXY_HOST:-0.0.0.0}"
export CLINE_PROXY_PORT="${CLINE_PROXY_PORT:-20129}"
echo "settings UI  http://127.0.0.1:${CLINE_PROXY_PORT}/"
echo "OpenAI API   http://127.0.0.1:${CLINE_PROXY_PORT}/v1"
exec python3 proxy.py
