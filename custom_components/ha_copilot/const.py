"""Constants for the HA-Copilot integration."""

DOMAIN = "ha_copilot"

# Configuration keys
CONF_BASE_URL = "base_url"
CONF_MODEL = "model"
CONF_API_KEY = "api_key"
CONF_TEMPERATURE = "temperature"
CONF_MAX_STEPS = "max_steps"
CONF_ALLOW_WRITE = "allow_write"
CONF_ALLOW_RESTART = "allow_restart"

# Defaults target a local Ollama OpenAI-compatible endpoint so no cloud key is
# required. Override any of these in configuration.yaml under `ha_copilot:`.
DEFAULT_BASE_URL = "http://localhost:11434/v1"
DEFAULT_MODEL = "qwen2.5:3b"
DEFAULT_TEMPERATURE = 0.2
DEFAULT_MAX_STEPS = 8
DEFAULT_ALLOW_WRITE = True
DEFAULT_ALLOW_RESTART = False

# Static asset routing for the sidebar panel.
PANEL_URL_PATH = "ha-copilot"
PANEL_TITLE = "HA-Copilot"
PANEL_ICON = "mdi:robot-happy-outline"
STATIC_URL_BASE = "/ha_copilot_static"

# HTTP API
API_CHAT = "/api/ha_copilot/chat"

DATA_STORE = "store"
