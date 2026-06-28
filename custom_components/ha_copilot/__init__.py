"""HA-Copilot: a capability layer fused into Home Assistant.

HA-Copilot bundles **no model** and calls **no external inference endpoint**. It
exposes the full Home Assistant operating surface as one deterministic tool layer
(:mod:`tools`), reachable through two foundations:

* Direct — the ``ha_copilot.run_tool`` service and the authenticated HTTP
  endpoints ``/api/ha_copilot/tools`` and ``/api/ha_copilot/run_tool``.
* MCP — the authenticated MCP server endpoint ``/api/ha_copilot/mcp``.

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


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the HA-Copilot capability layer."""
    conf = config.get(DOMAIN) or {}
    store = {
        CONF_ALLOW_WRITE: conf.get(CONF_ALLOW_WRITE, DEFAULT_ALLOW_WRITE),
        CONF_ALLOW_RESTART: conf.get(CONF_ALLOW_RESTART, DEFAULT_ALLOW_RESTART),
    }
    hass.data.setdefault(DOMAIN, {})[DATA_STORE] = store

    # HTTP surface: capability info, tool catalog, deterministic run, MCP server.
    hass.http.register_view(CopilotConfigView(hass))
    hass.http.register_view(CopilotToolsView(hass))
    hass.http.register_view(CopilotRunToolView(hass))
    hass.http.register_view(CopilotMcpView(hass))
    # Standard MCP HTTP+SSE transport so off-the-shelf MCP clients connect as-is.
    hass.http.register_view(CopilotMcpSseView(hass))
    hass.http.register_view(CopilotMcpMessagesView(hass))

    # Native LLM API: expose the deterministic tool layer to every conversation
    # agent (OpenAI / Anthropic / Google / local) via homeassistant.helpers.llm.
    async_register_llm_api(hass)

    # Serve the panel's static assets.
    panel_dir = os.path.join(os.path.dirname(__file__), "panel")
    await _register_static(hass, STATIC_URL_BASE, panel_dir)

    # Register the sidebar panel (a deterministic, model-free workspace).
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
        # Already registered (e.g. after a reload) - safe to ignore.
        pass

    # Directly invoke a single tool (no LLM). The canonical deterministic entry
    # point for automations/scripts and external operators.
    async def _handle_run_tool(call: ServiceCall) -> dict:
        return await dispatch_tool(
            hass, store, call.data["tool"], call.data.get("args") or {}
        )

    hass.services.async_register(
        DOMAIN,
        "run_tool",
        _handle_run_tool,
        schema=vol.Schema(
            {vol.Required("tool"): cv.string, vol.Optional("args"): dict}
        ),
        supports_response="only",
    )

    _LOGGER.info(
        "HA-Copilot ready - capability layer (write=%s restart=%s); "
        "drive via run_tool service, /api/ha_copilot/run_tool, MCP at "
        "/api/ha_copilot/mcp, or the native LLM API (any conversation agent)",
        store[CONF_ALLOW_WRITE],
        store[CONF_ALLOW_RESTART],
    )
    return True


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
