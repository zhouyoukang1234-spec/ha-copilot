"""HA-Copilot: a capability layer fused into Home Assistant.

HA-Copilot bundles **no model** and calls **no external inference endpoint**. It
exposes the full Home Assistant operating surface as one deterministic tool layer
(:mod:`tools`, 142 tools), reachable through four foundations:

* HA Services — ``ha_copilot.run_tool`` (generic) + 12 native resource services.
* MCP — the authenticated endpoint ``/api/ha_copilot/mcp`` (JSON-RPC 2.0).
* Native LLM API — registered via ``homeassistant.helpers.llm``, any conversation
  agent (OpenAI / Anthropic / Google / Ollama / local) selects HA-Copilot as its
  control API and gains all 142 deterministic tools.
* HTTP — ``/api/ha_copilot/tools`` and ``/api/ha_copilot/run_tool``.

The agent is always external (any MCP client / operator). Setup via
configuration.yaml is optional and only carries safety toggles:

    ha_copilot:
      allow_write: true      # let tools write config files
      allow_restart: false   # let tools restart Home Assistant
"""
from __future__ import annotations

import logging
import os

import voluptuous as vol

from homeassistant.components import panel_custom
from homeassistant.config_entries import ConfigEntry
import homeassistant.helpers.config_validation as cv
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_ALLOW_RESTART,
    CONF_ALLOW_WRITE,
    DATA_STORE,
    DEFAULT_ALLOW_RESTART,
    DEFAULT_ALLOW_WRITE,
    DOMAIN,
    PANEL_ICON,
    PANEL_TITLE,
    PANEL_URL_PATH,
    STATIC_URL_BASE,
)
from .http_api import (
    CopilotConfigView,
    CopilotMcpMessagesView,
    CopilotMcpSseView,
    CopilotMcpView,
    CopilotRunToolView,
    CopilotToolsView,
)
from .llm_api import async_register_llm_api
from .services import async_register_services
from .tools import dispatch as dispatch_tool

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(CONF_ALLOW_WRITE, default=DEFAULT_ALLOW_WRITE): cv.boolean,
                vol.Optional(CONF_ALLOW_RESTART, default=DEFAULT_ALLOW_RESTART): cv.boolean,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def _async_setup_core(
    hass: HomeAssistant, store: dict
) -> None:
    """Shared setup logic used by both YAML and config-entry paths."""
    # Guard against double-registration (YAML + config entry in same instance).
    if hass.data.get(DOMAIN, {}).get("_core_ready"):
        return
    hass.data.setdefault(DOMAIN, {})[DATA_STORE] = store
    hass.data[DOMAIN]["_core_ready"] = True

    # HTTP surface.
    hass.http.register_view(CopilotConfigView(hass))
    hass.http.register_view(CopilotToolsView(hass))
    hass.http.register_view(CopilotRunToolView(hass))
    hass.http.register_view(CopilotMcpView(hass))
    hass.http.register_view(CopilotMcpSseView(hass))
    hass.http.register_view(CopilotMcpMessagesView(hass))

    # Native LLM API.
    async_register_llm_api(hass)

    # Serve the panel's static assets.
    panel_dir = os.path.join(os.path.dirname(__file__), "panel")
    await _register_static(hass, STATIC_URL_BASE, panel_dir)

    # Sidebar panel.
    try:
        await panel_custom.async_register_panel(
            hass,
            webcomponent_name="ha-copilot-panel",
            frontend_url_path=PANEL_URL_PATH,
            module_url=f"{STATIC_URL_BASE}/panel.js",
            sidebar_title=PANEL_TITLE,
            sidebar_icon=PANEL_ICON,
            require_admin=True,
            embed_iframe=False,
        )
    except ValueError:
        pass

    # Generic run_tool service with event bus integration.
    async def _handle_run_tool(call: ServiceCall) -> dict:
        tool_name = call.data["tool"]
        result = await dispatch_tool(
            hass, store, tool_name, call.data.get("args") or {}
        )
        hass.bus.async_fire(
            f"{DOMAIN}_tool_called",
            {"tool": tool_name, "ok": result.get("ok", True) if isinstance(result, dict) else True},
        )
        return result

    hass.services.async_register(
        DOMAIN,
        "run_tool",
        _handle_run_tool,
        schema=vol.Schema(
            {vol.Required("tool"): cv.string, vol.Optional("args"): dict}
        ),
        supports_response="only",
    )

    # Individual named services for each resource-discovery tool.
    await async_register_services(hass)

    _LOGGER.info(
        "HA-Copilot ready - capability layer (write=%s restart=%s); "
        "4 routes: HA services / MCP / native LLM API / HTTP",
        store[CONF_ALLOW_WRITE],
        store[CONF_ALLOW_RESTART],
    )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up HA-Copilot from YAML configuration."""
    conf = config.get(DOMAIN) or {}
    store = {
        CONF_ALLOW_WRITE: conf.get(CONF_ALLOW_WRITE, DEFAULT_ALLOW_WRITE),
        CONF_ALLOW_RESTART: conf.get(CONF_ALLOW_RESTART, DEFAULT_ALLOW_RESTART),
    }
    await _async_setup_core(hass, store)
    return True


PLATFORMS: list[str] = ["sensor", "switch"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up HA-Copilot from a config entry (UI flow)."""
    store = {
        CONF_ALLOW_WRITE: entry.options.get(
            CONF_ALLOW_WRITE, DEFAULT_ALLOW_WRITE
        ),
        CONF_ALLOW_RESTART: entry.options.get(
            CONF_ALLOW_RESTART, DEFAULT_ALLOW_RESTART
        ),
    }
    await _async_setup_core(hass, store)

    # Forward sensor platform setup for diagnostic entities.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Live-update the store when the user changes options from the UI.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Handle options update — apply new safety toggles without restart."""
    store = hass.data.get(DOMAIN, {}).get(DATA_STORE)
    if store is not None:
        store[CONF_ALLOW_WRITE] = entry.options.get(
            CONF_ALLOW_WRITE, DEFAULT_ALLOW_WRITE
        )
        store[CONF_ALLOW_RESTART] = entry.options.get(
            CONF_ALLOW_RESTART, DEFAULT_ALLOW_RESTART
        )
        _LOGGER.info(
            "HA-Copilot options updated (write=%s restart=%s)",
            store[CONF_ALLOW_WRITE],
            store[CONF_ALLOW_RESTART],
        )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _register_static(hass: HomeAssistant, url_base: str, path: str) -> None:
    """Register a static directory, supporting both old and new HA APIs."""
    try:
        from homeassistant.components.http import StaticPathConfig

        await hass.http.async_register_static_paths(
            [StaticPathConfig(url_base, path, cache_headers=False)]
        )
    except ImportError:
        # Older Home Assistant cores.
        hass.http.register_static_path(url_base, path, cache_headers=False)
