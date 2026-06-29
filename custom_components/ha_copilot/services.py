"""Native Home Assistant services for HA-Copilot resource discovery.

Each resource tool becomes a first-class HA service with a typed
voluptuous schema and ``supports_response``, so automations can call
``ha_copilot.discover_resources`` directly (instead of routing through
the generic ``run_tool`` wrapper). Results are returned as service
response data.
"""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
import homeassistant.helpers.config_validation as cv

from . import resources
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def _query_schema(
    desc: str = "search query", limit_default: int = 10
) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required("query"): cv.string,
            vol.Optional("limit", default=limit_default): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=50)
            ),
        }
    )


_DISCOVER_SCHEMA = _query_schema("free-text query for all sources", 8)
_SEARCH_SCHEMA = _query_schema("brand or model", 10)
_RECOMMEND_SCHEMA = vol.Schema(
    {
        vol.Optional("limit", default=15): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=50)
        ),
        vol.Optional("include_blueprints", default=True): cv.boolean,
    }
)
_RECOMMEND_BP_SCHEMA = vol.Schema(
    {
        vol.Optional("limit", default=10): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=30)
        ),
    }
)


async def async_register_services(hass: HomeAssistant) -> None:
    """Register individual resource-discovery services."""

    async def _discover(call: ServiceCall) -> dict:
        return await resources.discover_resources(
            hass, call.data["query"], call.data["limit"]
        )

    async def _search_hacs(call: ServiceCall) -> dict:
        return await resources.search_community_resources(
            hass, call.data["query"], "all", call.data["limit"]
        )

    async def _search_github(call: ServiceCall) -> dict:
        return await resources.search_github(
            hass, call.data["query"], "stars", call.data["limit"]
        )

    async def _search_blueprints(call: ServiceCall) -> dict:
        return await resources.search_blueprints(
            hass, call.data["query"], call.data["limit"]
        )

    async def _search_zigbee(call: ServiceCall) -> dict:
        return await resources.search_zigbee_devices(
            hass, call.data["query"], call.data["limit"]
        )

    async def _search_zwave(call: ServiceCall) -> dict:
        return await resources.search_zwave_devices(
            hass, call.data["query"], call.data["limit"]
        )

    async def _search_tasmota(call: ServiceCall) -> dict:
        return await resources.search_tasmota_devices(
            hass, call.data["query"], call.data["limit"]
        )

    async def _search_esphome(call: ServiceCall) -> dict:
        return await resources.search_esphome_devices(
            hass, call.data["query"], call.data["limit"]
        )

    async def _search_integrations(call: ServiceCall) -> dict:
        return await resources.search_ha_integrations(
            hass, call.data["query"], call.data["limit"]
        )

    async def _search_addons(call: ServiceCall) -> dict:
        return await resources.search_ha_addons(
            hass, call.data["query"], call.data["limit"]
        )

    async def _recommend(call: ServiceCall) -> dict:
        return await resources.recommend_resources(
            hass, call.data["limit"], call.data["include_blueprints"]
        )

    async def _recommend_blueprints(call: ServiceCall) -> dict:
        return await resources.recommend_blueprints(
            hass, call.data["limit"]
        )

    services = [
        ("discover_resources", _discover, _DISCOVER_SCHEMA),
        ("search_hacs", _search_hacs, _SEARCH_SCHEMA),
        ("search_github", _search_github, _SEARCH_SCHEMA),
        ("search_blueprints", _search_blueprints, _SEARCH_SCHEMA),
        ("search_zigbee_devices", _search_zigbee, _SEARCH_SCHEMA),
        ("search_zwave_devices", _search_zwave, _SEARCH_SCHEMA),
        ("search_tasmota_devices", _search_tasmota, _SEARCH_SCHEMA),
        ("search_esphome_devices", _search_esphome, _SEARCH_SCHEMA),
        ("search_ha_integrations", _search_integrations, _SEARCH_SCHEMA),
        ("search_ha_addons", _search_addons, _SEARCH_SCHEMA),
        ("recommend_resources", _recommend, _RECOMMEND_SCHEMA),
        ("recommend_blueprints", _recommend_blueprints, _RECOMMEND_BP_SCHEMA),
    ]

    def _wrap_with_event(svc_name: str, handler):
        """Wrap a service handler to fire a bus event after each call."""
        async def _wrapped(call: ServiceCall) -> dict:
            result = await handler(call)
            hass.bus.async_fire(
                f"{DOMAIN}_tool_called",
                {
                    "tool": svc_name,
                    "ok": result.get("ok", True) if isinstance(result, dict) else True,
                },
            )
            return result
        return _wrapped

    for name, handler, schema in services:
        hass.services.async_register(
            DOMAIN,
            name,
            _wrap_with_event(name, handler),
            schema=schema,
            supports_response=SupportsResponse.ONLY,
        )

    _LOGGER.info(
        "HA-Copilot: %d native resource services registered", len(services)
    )
