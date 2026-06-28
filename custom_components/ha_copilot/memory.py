"""Agent memory for HA-Copilot.

A small, deterministic key/value memory that **persists across sessions and
restarts** so an external agent can remember what it learned about a home — the
user's devices, stated preferences, and notable past actions — instead of
re-discovering everything every conversation.

No model and no external calls: this is plain JSON persisted through Home
Assistant's own ``Store`` helper (``.storage/ha_copilot_memory``). Each entry is
namespaced by ``category`` and timestamped, so the agent can recall just
preferences, just the device profile, etc.
"""
from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .resources import _device_signals

_STORE_VERSION = 1
_STORE_KEY = "ha_copilot_memory"
_DATA_MEMORY = "memory_cache"
_DATA_MEMORY_STORE = "memory_store"


def _store(hass: HomeAssistant) -> Store:
    bucket = hass.data.setdefault(DOMAIN, {})
    store = bucket.get(_DATA_MEMORY_STORE)
    if store is None:
        store = Store(hass, _STORE_VERSION, _STORE_KEY)
        bucket[_DATA_MEMORY_STORE] = store
    return store


async def _load(hass: HomeAssistant) -> dict[str, Any]:
    bucket = hass.data.setdefault(DOMAIN, {})
    cache = bucket.get(_DATA_MEMORY)
    if cache is None:
        cache = await _store(hass).async_load() or {}
        bucket[_DATA_MEMORY] = cache
    return cache


async def _save(hass: HomeAssistant, data: dict[str, Any]) -> None:
    hass.data.setdefault(DOMAIN, {})[_DATA_MEMORY] = data
    await _store(hass).async_save(data)


async def remember(
    hass: HomeAssistant,
    key: str,
    value: Any,
    category: str = "general",
) -> dict[str, Any]:
    """Persist a single memory entry (upsert by ``key``)."""
    if not key:
        return {"error": "missing required argument: key"}
    data = dict(await _load(hass))
    data[key] = {
        "value": value,
        "category": category or "general",
        "updated_at": dt_util.utcnow().isoformat(),
    }
    await _save(hass, data)
    return {"ok": True, "key": key, "stored": data[key]}


async def recall(hass: HomeAssistant, key: str) -> dict[str, Any]:
    """Return one memory entry by ``key``."""
    if not key:
        return {"error": "missing required argument: key"}
    entry = (await _load(hass)).get(key)
    if entry is None:
        return {"ok": True, "key": key, "found": False}
    return {"ok": True, "key": key, "found": True, **entry}


async def list_memory(
    hass: HomeAssistant,
    category: str | None = None,
) -> dict[str, Any]:
    """List memory entries, optionally filtered to one ``category``."""
    data = await _load(hass)
    items = [
        {"key": k, **v}
        for k, v in data.items()
        if category is None or v.get("category") == category
    ]
    items.sort(key=lambda e: e.get("updated_at") or "", reverse=True)
    return {"ok": True, "count": len(items), "entries": items}


async def forget(hass: HomeAssistant, key: str) -> dict[str, Any]:
    """Delete one memory entry by ``key``."""
    if not key:
        return {"error": "missing required argument: key"}
    data = dict(await _load(hass))
    existed = key in data
    data.pop(key, None)
    await _save(hass, data)
    return {"ok": True, "key": key, "removed": existed}


async def snapshot_device_profile(hass: HomeAssistant) -> dict[str, Any]:
    """Capture the home's real device signals into memory.

    Stores the current manufacturers / integration domains / entity-domain
    counts under the ``devices`` category so later sessions can recall what the
    home contains without re-scanning, and so the agent can notice changes.
    """
    signals = _device_signals(hass)
    return await remember(hass, "device_profile", signals, category="devices")
