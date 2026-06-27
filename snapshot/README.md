# Home Assistant — Real Deployment Snapshot (sanitized)

A sanitized cloud snapshot of the user's real Home Assistant setup, redeployed and
operated end-to-end on Devin's VM, then driven entirely through the **native MCP
endpoint** exposed by the `ha_copilot` integration (`POST /api/ha_copilot/mcp`).

> 道法自然 · 无为而无不为 — one in-process native surface reaches everything; any
> agent (Devin or a third-party platform) drives HA through the same MCP endpoint.

## What's here

```
config/                 user's real HA config (YAML), secrets stripped
  configuration.yaml    main config (version-drift fixes applied for HA 2026.3.x)
  automations.yaml      30 automations
  scripts.yaml          scripts
  scenes.yaml           11 scenes
  template_sensors.yaml, groups.yaml, customize.yaml, ...
  lovelace/             dashboards incl. mobile-dashboard (mushroom-based)
  python_scripts/, themes/, blueprints/
  secrets.yaml.example  template — copy to secrets.yaml and fill in
custom_components/
  ha_copilot/           native deep-fusion integration + public MCP server
digital_twin/
  twin.py               MQTT digital twin: simulates the physical devices
                        (Sonoff plugs+power, switches, lights, fans, power
                        stations, temp/humidity + battery sensors) so the real
                        automations / energy pipeline / scenes run end-to-end
                        with no hardware.
tools/
  ha.py                 tiny HA REST + MCP client (ha.get / ha.tool / ha.mcp)
  op.py                 operate scenes via MCP and snapshot total power
```

## Excluded (never committed)

`.storage/` (auth, registries with tokens), `*.db*` (recorder), `*.log`,
`secrets.yaml`, third-party HACS integrations under `custom_components/` other
than `ha_copilot`, and `www/community/` vendor JS (mushroom, card-mod — install
via HACS).

## Run it

1. Home Assistant 2026.3.x with this `config/` mounted.
2. Mosquitto MQTT broker on `127.0.0.1:1883` (anonymous).
3. `python digital_twin/twin.py` — publishes MQTT discovery + retained state and
   echoes commands so every entity comes alive.
4. Drive it via MCP: `POST /api/ha_copilot/mcp` (JSON-RPC: `tools/list`,
   `tools/call`). See `custom_components/ha_copilot/MCP_NATIVE.md`.

## Defects found & fixed while operating the real config

1. `ws_exec`: `ActiveConnection.__init__` signature drift in HA 2026.3.4
   (`remote` removed, `refresh_token` required) — now introspected at runtime.
2. `utility_meter` source-id mismatch: Riemann energy sensors had pinyin
   entity_ids while meters expected `sensor.sonoff_<id>_energy` — realigned via
   the MCP registry, so daily/weekly/monthly meters accumulate again.
3. 13 registry orphans (UI-created automations/input_booleans with no YAML) —
   purged via MCP.
4. Mobile dashboard: missing mushroom/card-mod resources + a broken
   `!include` — resources remapped and missing twin sensors added so the
   dashboard renders fully with live data.
