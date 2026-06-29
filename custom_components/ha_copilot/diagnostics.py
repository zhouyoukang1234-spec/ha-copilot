"""Diagnostics support for HA-Copilot.

Accessed via Settings → Integrations → HA-Copilot card → Diagnostics.
Returns a sanitized snapshot of the integration's operational state.
"""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ALLOW_RESTART,
    CONF_ALLOW_WRITE,
    DATA_STORE,
    DOMAIN,
)
from .tools import TOOL_SPECS


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics data for the config entry."""
    store = hass.data.get(DOMAIN, {}).get(DATA_STORE, {})

    tool_names = []
    for spec in TOOL_SPECS:
        func = spec.get("function", {})
        name = func.get("name", "")
        if name:
            tool_names.append(name)

    return {
        "version": "0.3.0",
        "config_entry": {
            "entry_id": entry.entry_id,
            "options": {
                CONF_ALLOW_WRITE: entry.options.get(CONF_ALLOW_WRITE),
                CONF_ALLOW_RESTART: entry.options.get(CONF_ALLOW_RESTART),
            },
        },
        "store": {
            CONF_ALLOW_WRITE: store.get(CONF_ALLOW_WRITE),
            CONF_ALLOW_RESTART: store.get(CONF_ALLOW_RESTART),
        },
        "tools": {
            "total_count": len(TOOL_SPECS),
            "names": tool_names,
        },
        "data_sources": [
            "HACS (~2628 repos)",
            "GitHub search",
            "Community blueprints",
            "Zigbee devices (~2700)",
            "Z-Wave devices (~2375)",
            "Tasmota templates (~2800)",
            "ESPHome devices (~770)",
            "HA integrations (~1470)",
            "HA add-ons (78+)",
        ],
        "routes": [
            "HA services (run_tool + 12 resource services)",
            "MCP (JSON-RPC 2.0 at /api/ha_copilot/mcp)",
            "Native LLM API (homeassistant.helpers.llm)",
            "HTTP (/api/ha_copilot/run_tool)",
        ],
        "platforms": ["sensor", "switch"],
    }
