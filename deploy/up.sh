#!/usr/bin/env bash
# Bring up a full local HA-Copilot stack with Docker: Home Assistant + Ollama +
# the ha_copilot custom component, then onboard non-interactively.
#
# Idempotent and self-contained; safe to re-run. Override with env vars:
#   HA_DIR (default ~/ha-config), HA_MODEL (default qwen2.5:3b),
#   HA_PORT (8123), OLLAMA_PORT (11434).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HA_DIR="${HA_DIR:-$HOME/ha-config}"
HA_MODEL="${HA_MODEL:-qwen2.5:3b}"
HA_PORT="${HA_PORT:-8123}"
OLLAMA_PORT="${OLLAMA_PORT:-11434}"

echo "==> repo=$REPO_DIR  config=$HA_DIR  model=$HA_MODEL"
mkdir -p "$HA_DIR/custom_components"

# 1. Seed config (don't clobber an existing edited configuration.yaml).
[ -f "$HA_DIR/configuration.yaml" ] || cp "$REPO_DIR/deploy/configuration.yaml" "$HA_DIR/configuration.yaml"
for f in automations scripts scenes; do touch "$HA_DIR/$f.yaml"; done

# 2. Install / refresh the custom component from the repo. The HA container runs
# as root and leaves root-owned __pycache__ behind, so fall back to sudo.
rm -rf "$HA_DIR/custom_components/ha_copilot" 2>/dev/null \
  || sudo rm -rf "$HA_DIR/custom_components/ha_copilot"
cp -r "$REPO_DIR/custom_components/ha_copilot" "$HA_DIR/custom_components/ha_copilot"

# 3. Ollama (local LLM backend) + model.
docker rm -f ollama >/dev/null 2>&1 || true
docker run -d --name ollama -p "${OLLAMA_PORT}:11434" -v ollama:/root/.ollama ollama/ollama >/dev/null
echo "==> waiting for ollama"; sleep 5
docker exec ollama ollama pull "$HA_MODEL"

# 4. Home Assistant (host.docker.internal lets the container reach Ollama).
docker rm -f homeassistant >/dev/null 2>&1 || true
docker run -d --name homeassistant --add-host=host.docker.internal:host-gateway \
  -p "${HA_PORT}:8123" -v "$HA_DIR:/config" ghcr.io/home-assistant/home-assistant:stable >/dev/null
echo "==> waiting for Home Assistant to come up"
for i in $(seq 1 60); do
  curl -fsS "http://localhost:${HA_PORT}/manifest.json" >/dev/null 2>&1 && break
  sleep 2
done

# 5. Non-interactive onboarding -> writes ha_token.txt next to this script.
HA_TOKEN_FILE="${HA_TOKEN_FILE:-$REPO_DIR/deploy/ha_token.txt}" \
  python3 "$REPO_DIR/deploy/onboard.py"

echo "==> HA-Copilot is up:  http://localhost:${HA_PORT}/ha-copilot"
echo "    token: ${HA_TOKEN_FILE:-$REPO_DIR/deploy/ha_token.txt}"
