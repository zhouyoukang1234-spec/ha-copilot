"""HTTP API exposed to the HA-Copilot frontend panel."""
from __future__ import annotations

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .agent import run_agent
from .const import DATA_STORE, DOMAIN


class CopilotChatView(HomeAssistantView):
    """Handle chat turns from the sidebar panel."""

    url = "/api/ha_copilot/chat"
    name = "api:ha_copilot:chat"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def post(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except ValueError:
            return self.json({"error": "invalid JSON body"}, status_code=400)

        message = (body or {}).get("message")
        history = (body or {}).get("history") or []
        if not message or not isinstance(message, str):
            return self.json({"error": "'message' is required"}, status_code=400)

        store = self.hass.data[DOMAIN][DATA_STORE]
        result = await run_agent(self.hass, store, message, history)
        return self.json(result)


class CopilotConfigView(HomeAssistantView):
    """Expose the (non-secret) effective config so the panel can show status."""

    url = "/api/ha_copilot/config"
    name = "api:ha_copilot:config"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        store = self.hass.data[DOMAIN][DATA_STORE]
        return self.json(
            {
                "model": store.get("model"),
                "base_url": store.get("base_url"),
                "allow_write": store.get("allow_write"),
                "allow_restart": store.get("allow_restart"),
            }
        )
