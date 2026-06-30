import asyncio
import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components"))
import ha_copilot.tools as toolsmod  # noqa: E402
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
        components = set()
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
    check(r["ok"] and r["total_catalog"] == 2121 and any("energy" in n for n in names),
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
    check(r["ok"] and r["total"] == 2121 and r["read_only"] + r["write"] == 2121 and isinstance(r["groups"], dict),
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

    # 24) query_history without the recorder integration -> graceful error
    FH.config.components = set()
    r = await dispatch(hass, store, "query_history", {"domain": "sensor"})
    check("error" in r and "recorder" in r["error"],
          "query_history without recorder -> error", r)

    # 25) query_history happy path (recorder monkeypatched): per-entity numeric summary
    class FakeHist:
        @staticmethod
        def state_changes_during_period(h, start, end, eid):
            return {eid: [FS(eid, "10"), FS(eid, "20"), FS(eid, "30")]}

    class FakeInst:
        @staticmethod
        async def async_add_executor_job(fn, *a):
            return fn(*a)

    saved_hist, saved_inst = toolsmod._recorder_history, toolsmod._recorder_get_instance
    toolsmod._recorder_history = FakeHist
    toolsmod._recorder_get_instance = lambda h: FakeInst()
    FH.config.components = {"recorder"}
    try:
        r = await dispatch(hass, store, "query_history",
                           {"domain": "sensor", "device_class": "temperature", "hours": 6})
        ent = r["entities"][0] if r.get("entities") else {}
        num = ent.get("numeric", {})
        check(r.get("ok") and r["hours"] == 6 and ent.get("changes") == 3
              and num.get("first") == 10.0 and num.get("last") == 30.0
              and num.get("min") == 10.0 and num.get("max") == 30.0
              and num.get("delta") == 20.0,
              "query_history summarises recorder history (numeric stats)", r)

        # 26) samples returns recent raw points; same selection core as query_entities
        r = await dispatch(hass, store, "query_history",
                           {"domain": "sensor", "name_contains": "sleep", "samples": 2})
        ids = sorted(e["entity_id"] for e in r["entities"])
        check(r.get("ok") and ids == ["sensor.bedroom_sleep_score", "sensor.deep_sleep_hours"]
              and len(r["entities"][0].get("samples", [])) == 2,
              "query_history reuses selection core + returns samples", r)
    finally:
        toolsmod._recorder_history = saved_hist
        toolsmod._recorder_get_instance = saved_inst
        FH.config.components = set()

    # 27) apply_actions dry_run previews every step without acting
    FH.services.calls.clear()
    r = await dispatch(hass, store, "apply_actions", {
        "dry_run": True,
        "steps": [
            {"domain": "light", "state": "on", "service": "turn_off"},
            {"name_contains": "fan", "service": "switch.turn_off"},
        ],
    })
    check(r.get("dry_run") and r["steps_total"] == 2 and r["steps_run"] == 2
          and all(s.get("dry_run") for s in r["results"]) and len(FH.services.calls) == 0,
          "apply_actions dry_run previews all steps without acting", r)

    # 28) apply_actions runs an ordered batch (same core as control_entities)
    FH.services.calls.clear()
    r = await dispatch(hass, store, "apply_actions", {
        "steps": [
            {"domain": "light", "state": "on", "service": "turn_off"},
            {"name_contains": "fan", "domain": "switch", "service": "turn_off"},
        ],
    })
    called = set((d, s) for d, s, _ in FH.services.calls)
    check(r["ok"] and r["failed"] == 0 and ("light", "turn_off") in called
          and ("switch", "turn_off") in called,
          "apply_actions executes ordered batch via shared core", (r, FH.services.calls))

    # 29) apply_actions is write-gated; empty steps -> error
    FH.services.calls.clear()
    blocked = await dispatch(hass, {"allow_write": False}, "apply_actions",
                             {"steps": [{"domain": "light", "service": "turn_off"}]})
    empty = await dispatch(hass, store, "apply_actions", {"steps": []})
    check("error" in blocked and len(FH.services.calls) == 0 and "error" in empty,
          "apply_actions write-gated + empty-steps error", (blocked, empty))

    # 30) describe_entity: deep single-entity view (state + every attribute) by exact id
    r = await dispatch(hass, store, "describe_entity", {"entity_id": "light.kitchen"})
    check(r.get("ok") and r["entity_id"] == "light.kitchen" and r["domain"] == "light"
          and r["state"] == "on" and r["attributes"].get("brightness") == 200
          and r["friendly_name"] == "Kitchen Light",
          "describe_entity returns state + all attributes for exact id", r)

    # 31) describe_entity enriches with registry/device/area/related (registries patched)
    class FakeEntry:
        unique_id = "uid-kitchen"
        platform = "hue"
        device_id = "dev1"
        area_id = None
        entity_category = None
        disabled_by = None
        hidden_by = None
        original_name = "Kitchen"

    class FakeSelf:
        entity_id = "light.kitchen"
        device_id = "dev1"

    class FakeSibling:
        entity_id = "sensor.kitchen_power"
        device_id = "dev1"

    class FakeER:
        entities = {"light.kitchen": FakeSelf, "sensor.kitchen_power": FakeSibling}
        @staticmethod
        def async_get(eid): return FakeEntry

    class FakeDev:
        id = "dev1"
        name = "Kitchen Bulb"
        name_by_user = None
        manufacturer = "Signify"
        model = "LCT001"
        sw_version = "1.2"
        area_id = "area1"

    class FakeDR:
        @staticmethod
        def async_get(dev_id): return FakeDev

    class FakeArea:
        id = "area1"
        name = "Kitchen"

    class FakeAR:
        @staticmethod
        def async_get_area(aid): return FakeArea

    class _RegMod:
        def __init__(self, reg): self._reg = reg
        def async_get(self, h): return self._reg

    saved_er, saved_dr, saved_ar = toolsmod.er, toolsmod.dr, toolsmod.ar
    toolsmod.er, toolsmod.dr, toolsmod.ar = _RegMod(FakeER), _RegMod(FakeDR), _RegMod(FakeAR)
    try:
        r = await dispatch(hass, store, "describe_entity", {"entity_id": "light.kitchen"})
        check(r.get("ok") and r["registry"]["unique_id"] == "uid-kitchen"
              and r["device"]["manufacturer"] == "Signify"
              and r["area"]["name"] == "Kitchen"
              and r.get("related") == ["sensor.kitchen_power"],
              "describe_entity enriches with registry/device/area/related", r)
    finally:
        toolsmod.er, toolsmod.dr, toolsmod.ar = saved_er, saved_dr, saved_ar

    # 32) describe_entity fuzzy-resolves by name; unknown -> error
    r = await dispatch(hass, store, "describe_entity", {"name_contains": "fan"})
    miss = await dispatch(hass, store, "describe_entity", {"entity_id": "nope.nothere"})
    check(r.get("ok") and r["entity_id"] == "switch.fan" and "error" in miss,
          "describe_entity fuzzy-resolves by name; unknown -> error", (r, miss))

    # 33) aggregate_entities: explicit attribute="state" means the state value
    #     (live-practice refinement — an agent naturally passes attribute="state")
    over_state = await dispatch(hass, store, "aggregate_entities",
                                {"domain": "sensor", "device_class": "temperature",
                                 "attribute": "state"})
    over_none = await dispatch(hass, store, "aggregate_entities",
                               {"domain": "sensor", "device_class": "temperature"})
    check(over_state["ok"] and over_state["numeric_count"] >= 1
          and "avg" in over_state and over_state["avg"] == over_none["avg"],
          "aggregate_entities treats attribute='state' as the state value", over_state)

    # 34) apply_actions: selection may nest under select{} (flat keys still win)
    FH.services.calls.clear()
    r = await dispatch(hass, store, "apply_actions", {
        "steps": [
            {"select": {"domain": "light", "state": "on"}, "service": "turn_off"},
            {"select": {"name_contains": "fan", "domain": "switch"}, "service": "turn_off"},
        ],
    })
    called = set((d, s) for d, s, _ in FH.services.calls)
    check(r["ok"] and r["failed"] == 0 and r["results"][0]["count"] == 1
          and ("light", "turn_off") in called and ("switch", "turn_off") in called,
          "apply_actions honors nested select{} selection", (r, FH.services.calls))

    # 35) get_logbook: dispatch must pass entity_id/hours by the right name
    #     (live-practice: they were swapped positionally -> wrong filter + crash).
    #     With no logbook API, it falls back to state changes; filter must hold.
    r = await dispatch(hass, store, "get_logbook",
                       {"entity_id": "light.kitchen", "hours": 6})
    only = {e.get("entity_id") for e in r.get("entries", [])}
    check(r.get("ok") and only == {"light.kitchen"},
          "get_logbook honors entity_id filter (param order fixed)", r)
    # and a None hours must not crash (hardened to default)
    r2 = await dispatch(hass, store, "get_logbook", {"hours": None})
    check(r2.get("ok"), "get_logbook tolerates hours=None", r2)

    # 36) list_intent_handlers reduces handler objects to JSON-serialisable names
    #     (live-practice: returning handler objects 500'd at serialization).
    import json as _json

    import homeassistant.helpers.intent as _ih
    saved_get = _ih.async_get
    _ih.async_get = lambda h: [type("H", (), {"intent_type": "HassFoo"})(),
                               type("H", (), {"intent_type": "HassBar"})()]
    try:
        r = await dispatch(hass, store, "list_intent_handlers", {})
        serialisable = True
        try:
            _json.dumps(r)
        except TypeError:
            serialisable = False
        check(r.get("ok") and serialisable and r["intents"] == ["HassBar", "HassFoo"],
              "list_intent_handlers returns serialisable intent names", r)
    finally:
        _ih.async_get = saved_get

    # 37) trigger_automation guards a missing entity (live-practice: HA's
    #     automation.trigger silently no-ops on a missing target -> false ok).
    FH.services.calls.clear()
    miss = await dispatch(hass, store, "trigger_automation",
                          {"entity_id": "automation.nope"})
    hass.states._s["automation.demo"] = FS("automation.demo", "on",
                                            {"friendly_name": "Demo"})
    hit = await dispatch(hass, store, "trigger_automation",
                         {"entity_id": "automation.demo"})
    check("error" in miss and hit.get("ok")
          and ("automation", "trigger") in {(d, s) for d, s, _ in FH.services.calls},
          "trigger_automation errors on missing entity, fires on real one", (miss, hit))

    # 38/39) create_scene normalises scalar states (int/float->str, bool->on/off)
    #        so one non-string value can't invalidate scenes.yaml; delete_scene
    #        accepts the 'scene.<id>' entity_id form (live-practice refinements).
    import yaml as _yaml
    os.makedirs("/tmp/ha", exist_ok=True)
    scenes_path = "/tmp/ha/scenes.yaml"
    with open(scenes_path, "w") as _fp:
        _fp.write("[]\n")

    class _FakeReg:
        @staticmethod
        def async_get(eid): return None
        @staticmethod
        def async_remove(eid): pass

    saved_er2 = toolsmod.er
    toolsmod.er = type("M", (), {"async_get": staticmethod(lambda h: _FakeReg)})
    try:
        cr = await dispatch(hass, store, "create_scene",
                            {"name": "dao_test_scene",
                             "entities": {"input_number.t": 21, "switch.x": True}})
        ents = (_yaml.safe_load(open(scenes_path)) or [{}])[0].get("entities", {})
        check(cr.get("ok") and ents.get("input_number.t") == "21"
              and ents.get("switch.x") == "on",
              "create_scene normalises scalar states to scene-valid strings", (cr, ents))

        dl = await dispatch(hass, store, "delete_scene",
                            {"entity_id": "scene.dao_test_scene"})
        remaining = _yaml.safe_load(open(scenes_path)) or []
        check(dl.get("ok") and dl.get("removed") == 1 and remaining == [],
              "delete_scene accepts 'scene.<id>' entity_id form", (dl, remaining))
    finally:
        toolsmod.er = saved_er2

    # 40) entity_id narrows the shared selection core for every primitive
    #     (live-practice: query_history ignored entity_id -> returned ALL
    #     entities' history; the fix lives once in _select_entities).
    q = await dispatch(hass, store, "query_entities",
                       {"entity_id": "switch.fan"})
    a = await dispatch(hass, store, "aggregate_entities",
                       {"entity_id": "switch.fan"})
    check(q.get("ok") and q["count"] == 1
          and q["entities"][0]["entity_id"] == "switch.fan"
          and a.get("ok") and a["count"] == 1,
          "entity_id narrows query/aggregate to exactly that entity", (q, a))

    # 41) describe_entity resolves through the shared selection core: a bare
    #     domain (no entity_id/name) narrows to that domain rather than falling
    #     through to the first entity of everything (live-practice defect).
    r = await dispatch(hass, store, "describe_entity", {"domain": "switch"})
    check(r.get("ok") and r["domain"] == "switch",
          "describe_entity honors domain when no entity_id/name given", r)

    # 42) automation_describe reads triggers from the stored config, not from
    #     state attributes (HA never exposes trigger/condition/action as attrs),
    #     and normalises a single-mapping trigger block to a list (live-practice
    #     defect: every automation reported trigger_count 0 / trigger_types []).
    os.makedirs("/tmp/ha", exist_ok=True)
    with open("/tmp/ha/automations.yaml", "w", encoding="utf-8") as fh_:
        fh_.write(
            "- id: dao_t1\n"
            "  alias: Dao T1\n"
            "  trigger:\n"
            "    platform: state\n"
            "    entity_id: input_boolean.fan_switch\n"
            "    to: 'on'\n"
            "  condition:\n"
            "  - condition: state\n"
            "    entity_id: input_boolean.living_room_lamp\n"
            "    state: 'on'\n"
            "  action:\n"
            "  - service: switch.turn_on\n"
            "    target:\n"
            "      entity_id: switch.ac\n"
        )
    hass.states._s["automation.dao_t1"] = FS(
        "automation.dao_t1", "on", {"friendly_name": "Dao T1", "id": "dao_t1"})
    ad = await dispatch(hass, store, "automation_describe",
                        {"entity_id": "automation.dao_t1"})
    check(ad.get("ok") and ad["trigger_count"] == 1
          and ad["trigger_types"] == ["state"]
          and ad["condition_count"] == 1 and ad["action_count"] == 1,
          "automation_describe reads single-mapping trigger from config", ad)

    # 43) get_statistics must honour its declared statistic_ids contract. A
    #     duplicate _get_statistics definition (entity_id-based) silently
    #     shadowed the intended one, so the dispatched call bound the wrong
    #     positional args and crashed (live-practice defect). Guard against the
    #     duplicate-shadowing class by introspecting the resolved function.
    import inspect
    params = list(inspect.signature(toolsmod._get_statistics).parameters)
    check(params[:2] == ["hass", "statistic_ids"],
          "get_statistics resolves to the statistic_ids contract (no dup shadow)",
          params)

    print(f"\n=== RESULTS: {p}/{p+f} passed ===")
    return f == 0


ok = asyncio.run(main())
sys.exit(0 if ok else 1)
