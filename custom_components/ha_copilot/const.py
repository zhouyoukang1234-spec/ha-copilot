"""Constants for HA-Copilot.

HA-Copilot is a *pure capability layer* fused into Home Assistant. It bundles no
model and calls no external inference endpoint. The intelligence (the agent) is
whatever external operator drives it — directly via the ``run_tool`` service /
HTTP, or through the built-in MCP server endpoint. This file therefore carries
no model/base_url/api_key configuration; only capability-safety toggles remain.
"""

DOMAIN = "ha_copilot"

# Capability-safety toggles (NOT model config).
CONF_ALLOW_WRITE = "allow_write"
CONF_ALLOW_RESTART = "allow_restart"

DEFAULT_ALLOW_WRITE = True
DEFAULT_ALLOW_RESTART = False

# Static asset routing for the sidebar panel.
PANEL_URL_PATH = "ha-copilot"
PANEL_TITLE = "HA-Copilot"
PANEL_ICON = "mdi:hexagon-multiple-outline"
STATIC_URL_BASE = "/ha_copilot_static"

# HTTP API surface (authenticated). These let any external agent drive the tool
# layer over plain HTTP, and expose an MCP server endpoint over the same layer.
API_TOOLS = "/api/ha_copilot/tools"
API_RUN_TOOL = "/api/ha_copilot/run_tool"
API_MCP = "/api/ha_copilot/mcp"

DATA_STORE = "store"
