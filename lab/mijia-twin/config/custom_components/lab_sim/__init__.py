"""Lab Sim — a digital-twin device layer for Home Assistant.

This integration carries no model and talks to no external endpoint. It simply
materialises *mock* entities for every device the user's real Mijia config
references but which is not produced locally (Xiaomi Home / MQTT / EcoFlow /
…). It only ever creates an entity if that exact ``entity_id`` does not already
exist, so it never collides with template sensors, helpers or any real
integration — it purely fills the gaps to make the twin whole.
"""

from __future__ import annotations

import json
import logging
import os

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers.discovery import async_load_platform
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, PLATFORMS

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Lab Sim twin from YAML."""
    conf = config.get(DOMAIN) or {}

    path = os.path.join(os.path.dirname(__file__), "entities.json")

    def _load() -> dict[str, list[str]]:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    manifest: dict[str, list[str]] = await hass.async_add_executor_job(_load)

    extra = conf.get("entities") or {}
    for domain, ids in extra.items():
        manifest.setdefault(domain, []).extend(ids)

    hass.data[DOMAIN] = manifest

    async def _spawn(_event: Event | None = None) -> None:
        for platform in PLATFORMS:
            ids = manifest.get(platform) or []
            missing = [eid for eid in ids if hass.states.get(eid) is None]
            if not missing:
                continue
            _LOGGER.info("lab_sim: spawning %d %s twin entities", len(missing), platform)
            hass.async_create_task(
                async_load_platform(
                    hass, platform, DOMAIN, {"entities": missing}, config
                )
            )

    # Spawn immediately so the twin devices exist before automations / scripts
    # are validated, then top up once more after start to catch anything that a
    # late-loading integration was still expected to provide.
    await _spawn()
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _spawn)

    return True
