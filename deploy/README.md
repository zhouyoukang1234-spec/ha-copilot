# HA-Copilot dev harness

Spin up a complete local stack — Home Assistant + Ollama (local LLM) + the
`ha_copilot` custom component — with one command, then drive it from the
sidebar **HA-Copilot** panel or the `ha_copilot.ask` / `ha_copilot.run_tool`
services.

## Requirements
- Docker
- Python 3 (stdlib only)

## Bring it up
```bash
bash deploy/up.sh
```
This pulls the `ollama/ollama` and `home-assistant:stable` images, pulls the
`qwen2.5:3b` model, seeds `~/ha-config` with `configuration.yaml` (demo lights,
AC, the `ha_copilot` integration), installs the component, and onboards a
`devin` / `devin-ha-2026` owner user non-interactively. The access token is
written to `deploy/ha_token.txt`.

Open http://localhost:8123/ha-copilot and try `把客厅灯打开` /
`turn on the living room light`.

## Smoke-test every tool
```bash
HA_TOKEN_FILE=deploy/ha_token.txt python3 deploy/tool_smoketest.py
```
Runs a deterministic closed-loop test of all `run_tool` operations (state and
service calls, file I/O, config check, automation/scene/script creation,
registry writes, templates, history, path-escape safety). Expect `23/23 passed`.

## Useful env overrides
`HA_DIR`, `HA_MODEL`, `HA_PORT`, `OLLAMA_PORT`, `HA_USERNAME`, `HA_PASSWORD`.
