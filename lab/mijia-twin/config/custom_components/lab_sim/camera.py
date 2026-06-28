"""Mock cameras for the Lab Sim twin.

Returns a tiny static PNG so the camera entity is fully valid in the UI
without any real device or stream.
"""

from __future__ import annotations

import base64

from homeassistant.components.camera import Camera
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .util import humanize

# 1x1 dark-grey PNG.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


async def async_setup_platform(
    hass: HomeAssistant, config, async_add_entities: AddEntitiesCallback, discovery_info=None
) -> None:
    if not discovery_info:
        return
    async_add_entities(SimCamera(eid) for eid in discovery_info["entities"])


class SimCamera(Camera):
    _attr_should_poll = False

    def __init__(self, entity_id: str) -> None:
        super().__init__()
        self.entity_id = entity_id
        self._attr_unique_id = f"lab_sim_{entity_id}"
        self._attr_name = humanize(entity_id)

    async def async_camera_image(self, width=None, height=None) -> bytes:
        return _PNG
