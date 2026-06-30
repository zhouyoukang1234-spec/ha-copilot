import asyncio
import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components"))
from ha_copilot.tools import dispatch  # noqa: E402

NOW = datetime.datetime.now(datetime.timezone.utc)


class FS:
    def __init__(self, eid, state, attrs=None):
        self.entity_id = eid
        self.state = state
        self.attributes = attrs or {}
        self.last_changed = NOW
        self.last_updated = NOW


class FB:
    def async_fire(self, *a, **k): pass
    def async_listeners(self): return {}


class FH:
    class config:
        config_dir = "/tmp/ha"
        version = "2024.12.0"
    class states:
        _s = {}
        @classmethod
        def async_all(cls, domain=None):
            return [s for s in cls._s.values()
                    if not domain or s.entity_id.startswith(f"{domain}.")]
        @classmethod
        def get(cls, eid): return cls._s.get(eid)
    class services:
        calls = []
        _known = {("light", "turn_off"), ("light", "turn_on"), ("switch", "turn_off"),
                  ("homeassistant", "turn_off"), ("cover", "close_cover")}
        @staticmethod
        def async_services(): return {}
        @classmethod
        def has_service(cls, dom, svc): return (dom, svc) in cls._known
        @classmethod
        async def async_call(cls, dom, svc, data=None, blocking=False, **k):
            cls.calls.append((dom, svc, data))
    bus = FB()
    data = {}
    @staticmethod
    async def async_add_executor_job(fn, *a): return fn(*a)


hass = FH()
for e in [
    FS("sensor.bedroom_sleep_score", "88", {"friendly_name": "Bedroom Sleep Score", "unit_of_measurement": "pts"}),
    FS("sensor.deep_sleep_hours", "2.1", {"friendly_name": "Deep Sleep Hours", "unit_of_measurement": "h"}),
    FS("sensor.body_weight", "72.5", {"friendly_name": "Body Weight", "device_class": "weight", "unit_of_measurement": "kg"}),
    FS("sensor.bathroom_scale", "71.9", {"friendly_name": "Bathroom Scale Weight", "unit_of_measurement": "kg"}),
    FS("sensor.living_temp", "22.5", {"friendly_name": "Living Temp", "device_class": "temperature", "unit_of_measurement": "°C"}),
    FS("light.kitchen", "on", {"friendly_name": "Kitchen Light", "brightness": 200}),
    FS("light.hall", "off", {"friendly_name": "Hall Light"}),
    FS("switch.fan", "on", {"friendly_name": "Fan"}),
]:
    hass.states._s[e.entity_id] = e

store = {"allow_write": True}
p = f = 0


def check(cond, label, detail=""):
    global p, f
    if cond:
        p += 1
        print(f"  [PASS] {label}")
    else:
        f += 1
        print(f"  [FAIL] {label} — {detail}")


async def main():
    print("=== 為道日損 · source-primitive tests ===")

    # 1) query_entities reproduces _sleep_tracker_status (domain=sensor, name 'sleep')
    direct = await dispatch(hass, store, "sleep_tracker_status", {})
    prim = await dispatch(hass, store, "query_entities", {"domain": "sensor", "name_contains": "sleep"})
    d_ids = sorted(x["entity_id"] for x in direct["sensors"])
    p_ids = sorted(x["entity_id"] for x in prim["entities"])
    check(d_ids == p_ids and d_ids == ["sensor.bedroom_sleep_score", "sensor.deep_sleep_hours"],
          "query_entities == sleep_tracker_status", f"{d_ids} vs {p_ids}")

    # 2) reproduce _weight_tracker_status (device_class weight OR name weight/scale)
    direct = await dispatch(hass, store, "weight_tracker_status", {})
    prim = await dispatch(hass, store, "query_entities",
                          {"domain": "sensor", "device_class": "weight",
                           "name_contains": ["weight", "scale"], "match": "any"})
    d_ids = sorted(x["entity_id"] for x in direct["sensors"])
    p_ids = sorted(x["entity_id"] for x in prim["entities"])
    check(set(["sensor.body_weight", "sensor.bathroom_scale"]).issubset(set(p_ids)),
          "query_entities reproduces weight_tracker_status set", f"direct={d_ids} prim={p_ids}")

    # 3) device_class filter
    r = await dispatch(hass, store, "query_entities", {"device_class": "temperature"})
    check(r["count"] == 1 and r["entities"][0]["entity_id"] == "sensor.living_temp",
          "query_entities device_class=temperature", r)

    # 4) state filter (lights that are on)
    r = await dispatch(hass, store, "query_entities", {"domain": "light", "state": "on"})
    check(r["count"] == 1 and r["entities"][0]["entity_id"] == "light.kitchen",
          "query_entities domain=light state=on", r)

    # 5) attribute presence filter (brightness present)
    r = await dispatch(hass, store, "query_entities", {"domain": "light", "attributes": {"brightness": None}})
    check(r["count"] == 1 and r["entities"][0]["entity_id"] == "light.kitchen",
          "query_entities attributes brightness present", r)

    # 6) match=all (name AND device_class) — body_weight has both
    r = await dispatch(hass, store, "query_entities",
                       {"domain": "sensor", "device_class": "weight", "name_contains": "body", "match": "all"})
    check(r["count"] == 1 and r["entities"][0]["entity_id"] == "sensor.body_weight",
          "query_entities match=all (name AND device_class)", r)

    # 7) no filters returns everything in domain
    r = await dispatch(hass, store, "query_entities", {"domain": "light"})
    check(r["count"] == 2, "query_entities domain-only returns all", r)

    # 8) search_tools finds the energy tools by intent
    r = await dispatch(hass, store, "search_tools", {"query": "energy usage", "limit": 10})
    names = [t["name"] for t in r["tools"]]
    check(r["ok"] and r["total_catalog"] == 2118 and any("energy" in n for n in names),
          "search_tools 'energy usage' returns energy tools", names[:5])

    # 9) search_tools finds query_entities itself
    r = await dispatch(hass, store, "search_tools", {"query": "find entities by device class"})
    check(any(t["name"] == "query_entities" for t in r["tools"]),
          "search_tools surfaces query_entities", [t["name"] for t in r["tools"][:5]])

    # 10) describe_tool returns the full schema
    r = await dispatch(hass, store, "describe_tool", {"name": "query_entities"})
    check(r.get("ok") and "parameters" in r["tool"] and r["annotations"]["readOnlyHint"] is True,
          "describe_tool query_entities schema + read-only annotation", r.get("error"))

    # 11) describe_tool unknown
    r = await dispatch(hass, store, "describe_tool", {"name": "nope_not_real"})
    check("error" in r, "describe_tool unknown -> error", r)

    # 12) tool_catalog overview
    r = await dispatch(hass, store, "tool_catalog", {})
    check(r["ok"] and r["total"] == 2118 and r["read_only"] + r["write"] == 2118 and isinstance(r["groups"], dict),
          "tool_catalog overview totals consistent", r)

    # 13) tool_catalog prefix listing
    r = await dispatch(hass, store, "tool_catalog", {"prefix": "list_"})
    check(r["ok"] and r["count"] > 0 and all(n.startswith("list_") for n in r["tools"]),
          "tool_catalog prefix=list_", r["count"])

    # 14) all new primitives are read-only (no write gate needed) — call under allow_write=false
    ro_store = {"allow_write": False}
    r = await dispatch(hass, ro_store, "query_entities", {"domain": "light"})
    check(r.get("ok") is True, "query_entities works under allow_write=false", r)

    # 15) control_entities dry_run previews targets, no service called
    FH.services.calls.clear()
    r = await dispatch(hass, store, "control_entities",
                       {"domain": "light", "state": "on", "service": "turn_off", "dry_run": True})
    check(r.get("dry_run") and r["count"] == 1 and r["targets"][0]["entity_id"] == "light.kitchen"
          and len(FH.services.calls) == 0,
          "control_entities dry_run previews without acting", r)

    # 16) control_entities bare service grouped per entity domain
    FH.services.calls.clear()
    r = await dispatch(hass, store, "control_entities",
                       {"domain": "light", "state": "on", "service": "turn_off"})
    check(r["ok"] and r["count"] == 1 and ("light", "turn_off", {"entity_id": ["light.kitchen"]}) in FH.services.calls,
          "control_entities bare service acts on matched lights", (r, FH.services.calls))

    # 17) control_entities qualified service applied to all matches
    FH.services.calls.clear()
    r = await dispatch(hass, store, "control_entities",
                       {"name_contains": ["kitchen", "hall"], "service": "light.turn_on",
                        "data": {"brightness_pct": 40}})
    called = FH.services.calls[0] if FH.services.calls else None
    check(r["ok"] and called and called[0] == "light" and called[1] == "turn_on"
          and called[2].get("brightness_pct") == 40 and set(called[2]["entity_id"]) == {"light.hall", "light.kitchen"},
          "control_entities qualified service + data on all matches", (r, FH.services.calls))

    # 18) control_entities respects allow_write=false (but dry_run still allowed)
    FH.services.calls.clear()
    r = await dispatch(hass, {"allow_write": False}, "control_entities",
                       {"domain": "light", "service": "turn_off"})
    blocked = "error" in r and len(FH.services.calls) == 0
    r2 = await dispatch(hass, {"allow_write": False}, "control_entities",
                        {"domain": "light", "service": "turn_off", "dry_run": True})
    check(blocked and r2.get("dry_run") is True,
          "control_entities write-gated; dry_run still allowed under allow_write=false", (r, r2))

    # 19) control_entities no match -> ok, no calls
    FH.services.calls.clear()
    r = await dispatch(hass, store, "control_entities",
                       {"domain": "light", "name_contains": "nonexistent", "service": "turn_off"})
    check(r["ok"] and r["count"] == 0 and len(FH.services.calls) == 0,
          "control_entities no-match is a safe no-op", r)

    # 20) control_entities missing service -> error
    r = await dispatch(hass, store, "control_entities", {"domain": "light"})
    check("error" in r, "control_entities missing service -> error", r)

    # 21) aggregate_entities counts + on_count over a domain
    r = await dispatch(hass, store, "aggregate_entities", {"domain": "light"})
    check(r["ok"] and r["count"] == 2 and r["on_count"] == 1,
          "aggregate_entities count + on_count for lights", r)

    # 22) aggregate_entities numeric sum/avg/min/max over sensor states
    r = await dispatch(hass, store, "aggregate_entities",
                       {"domain": "sensor", "device_class": "temperature"})
    check(r["ok"] and r["numeric_count"] >= 1 and "avg" in r and r["min"] <= r["max"],
          "aggregate_entities numeric stats over temperature sensors", r)

    # 23) aggregate_entities over an attribute instead of state
    r = await dispatch(hass, store, "aggregate_entities",
                       {"domain": "light", "attribute": "brightness"})
    check(r["ok"] and r["over"] == "brightness" and r["numeric_count"] >= 1,
          "aggregate_entities aggregates over an attribute", r)

    print(f"\n=== RESULTS: {p}/{p+f} passed ===")
    return f == 0


ok = asyncio.run(main())
sys.exit(0 if ok else 1)
