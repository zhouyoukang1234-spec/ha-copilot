"""HA-Copilot: an AI co-pilot fused into Home Assistant.

Setup via configuration.yaml:

    ha_copilot:
      base_url: "http://localhost:11434/v1"   # any OpenAI-compatible endpoint
      model: "qwen2.5:3b"
      # api_key: "sk-..."                      # only for cloud endpoints
      allow_write: true
      allow_restart: false
"""
from __future__ import annotations

import logging
import os

import voluptuous as vol

from homeassistant.components import panel_custom
import homeassistant.helpers.config_validation as cv
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.typing import ConfigType

from .agent import run_agent
from .const import (
    CONF_ALLOW_RESTART,
    CONF_ALLOW_WRITE,
    CONF_API_KEY,
    CONF_BASE_URL,
    CONF_MAX_STEPS,
    CONF_MODEL,
    CONF_TEMPERATURE,
    DATA_STORE,
    DEFAULT_ALLOW_RESTART,
    DEFAULT_ALLOW_WRITE,
    DEFAULT_BASE_URL,
    DEFAULT_MAX_STEPS,
    DEFAULT_MODEL,
    DEFAULT_TEMPERATURE,
    DOMAIN,
    PANEL_ICON,
    PANEL_TITLE,
    PANEL_URL_PATH,
    STATIC_URL_BASE,
)
from .http_api import CopilotChatView, CopilotConfigView
from .tools import dispatch as dispatch_tool

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(CONF_BASE_URL, default=DEFAULT_BASE_URL): cv.string,
                vol.Optional(CONF_MODEL, default=DEFAULT_MODEL): cv.string,
                vol.Optional(CONF_API_KEY): cv.string,
                vol.Optional(CONF_TEMPERATURE, default=DEFAULT_TEMPERATURE): vol.Coerce(float),
                vol.Optional(CONF_MAX_STEPS, default=DEFAULT_MAX_STEPS): cv.positive_int,
                vol.Optional(CONF_ALLOW_WRITE, default=DEFAULT_ALLOW_WRITE): cv.boolean,
                vol.Optional(CONF_ALLOW_RESTART, default=DEFAULT_ALLOW_RESTART): cv.boolean,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up HA-Copilot from configuration.yaml."""
    conf = config.get(DOMAIN) or {}
    store = {
        CONF_BASE_URL: conf.get(CONF_BASE_URL, DEFAULT_BASE_URL),
        CONF_MODEL: conf.get(CONF_MODEL, DEFAULT_MODEL),
        CONF_API_KEY: conf.get(CONF_API_KEY),
        CONF_TEMPERATURE: conf.get(CONF_TEMPERATURE, DEFAULT_TEMPERATURE),
        CONF_MAX_STEPS: conf.get(CONF_MAX_STEPS, DEFAULT_MAX_STEPS),
        CONF_ALLOW_WRITE: conf.get(CONF_ALLOW_WRITE, DEFAULT_ALLOW_WRITE),
        CONF_ALLOW_RESTART: conf.get(CONF_ALLOW_RESTART, DEFAULT_ALLOW_RESTART),
    }
    hass.data.setdefault(DOMAIN, {})[DATA_STORE] = store

    # HTTP API for the panel.
    hass.http.register_view(CopilotChatView(hass))
    hass.http.register_view(CopilotConfigView(hass))

    # Serve the panel's static assets.
    panel_dir = os.path.join(os.path.dirname(__file__), "panel")
    await _register_static(hass, STATIC_URL_BASE, panel_dir)

    # Register the sidebar panel (a custom web component).
    try:
        await panel_custom.async_register_panel(
            hass,
            webcomponent_name="ha-copilot-panel",
            frontend_url_path=PANEL_URL_PATH,
            module_url=f"{STATIC_URL_BASE}/panel.js",
            sidebar_title=PANEL_TITLE,
            sidebar_icon=PANEL_ICON,
            require_admin=True,
            config={"base_url": store[CONF_BASE_URL], "model": store[CONF_MODEL]},
            embed_iframe=False,
        )
    except ValueError:
        # Already registered (e.g. after a reload) - safe to ignore.
        pass

    # A service so the copilot can also be driven from automations / dev-tools.
    async def _handle_ask(call: ServiceCall) -> dict:
        result = await run_agent(hass, store, call.data["message"], [])
        return {"reply": result.get("reply", ""), "steps": result.get("steps", [])}

    hass.services.async_register(
        DOMAIN,
        "ask",
        _handle_ask,
        schema=vol.Schema({vol.Required("message"): cv.string}),
        supports_response="only",
    )

    # Directly invoke a single copilot tool (bypassing the LLM). Useful from
    # automations/scripts that already know exactly which low-level operation
    # they want, and for deterministic testing of the tool layer.
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
        "HA-Copilot ready - model=%s endpoint=%s (write=%s restart=%s)",
        store[CONF_MODEL],
        store[CONF_BASE_URL],
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
