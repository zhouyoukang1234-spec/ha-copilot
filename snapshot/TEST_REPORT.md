# Operational Test Report — Real HA driven via native MCP

**Target:** the user's real Home Assistant config, redeployed on Devin's VM
(HA 2026.3.4, Docker) and operated entirely through the native MCP endpoint
`POST /api/ha_copilot/mcp`.

**Result:** all operational tests passed. 257 entities loaded, 0 functional
entities unavailable (digital twin), 30/30 automations enabled and firing,
energy pipeline accumulating, mushroom dashboards rendering live.

## Infrastructure

| Piece | State |
|-------|-------|
| Home Assistant | 2026.3.4, RUNNING, "My Home", Asia/Urumqi |
| Entities | 257 loaded |
| MQTT digital twin | Sonoff plugs+power, switches, lights, fans, power stations, temp/humidity + battery sensors |
| Native MCP | `/api/ha_copilot/mcp`, 29 tools, JSON-RPC over HTTP |

## Tests

### 1. Mobile dashboard renders live (digital-twin backed)
The user's `移动端控制中心` (mushroom) dashboard renders fully with live chips,
light/switch cards, power gauges and battery levels.

![mobile dashboard](docs/evidence/01_mobile_dashboard_live.png)

### 2. Bulk light control via MCP
10 `light.turn_on` / `light.turn_off` calls issued through the MCP `call_service`
tool. All cards reflected state; total power tracked the change live
(132W → 26W on all-off).

![all lights on via MCP](docs/evidence/02_all_lights_on_via_mcp.png)

### 3. Energy pipeline live (Riemann → utility_meter)
`能源监控` view shows live total-power trend (~26W) + gauges; cumulative
`sensor.sonoff_total_energy` accumulating (0.068 kWh observed and rising).

![energy view](docs/evidence/03_energy_view_live.png)

### 4. Automations loaded and firing
All **30** of the user's automations enabled. Several show `Last triggered`
2–5 minutes ago — fired in response to the MCP-driven device changes
(e.g. `打开四号时打开五号`, `关闭4号时自动关闭5号`).

![30 automations](docs/evidence/04_automations_30_enabled.png)

### 5. Both MCP routes proven on the real config

Same real deployment, exercised through both routes end-to-end:

- **Route B (native, in-process)** — `native_selfcheck.py` against
  `/api/ha_copilot/mcp`: **21/21** (states, services, template, history,
  registry, areas/labels/floors, dashboards, helpers, automation/scene/script
  CRUD with yaml restore, users, config_entries, system_health, universal ws).
- **Route A (external bridge)** — `ha_mcp.selfcheck` spawning the bridge as a
  subprocess and driving the live HA: **29/29** (after a fix — see below).

### 6. User's real protection automation fired end-to-end

Drove the chain on the user's own logic, no special-casing:
injected an >800W spike on one plug's power sensor via MQTT →
`sensor.sonoff_total_power_usage` = 876W → the user's template-trigger
automation `功率超600关闭户外电源插头` fired (`last_triggered` None→`12:02:04`)
→ `switch.sonoff_100235142b_1` (outdoor power) auto-turned **off**. Cleared the
spike; total self-healed back to ~25W via the twin's jitter loop.

```
MQTT 850W ─▶ twin/sonoff_..._power/state
          ─▶ template sensor sonoff_total_power_usage = 876W
          ─▶ template trigger (> 600)  ─▶ automation
          ─▶ switch.turn_off outdoor plug  ✓ (plug = off, last_triggered set)
```

## Defects found & fixed

| # | Defect | Fix |
|---|--------|-----|
| 1 | `ha_ws` crashed: `ActiveConnection.__init__` signature drift (HA 2026.3.4 removed `remote`, made `refresh_token` required) | Introspect signature at runtime; pass only accepted params; reuse a real refresh token or minimal stand-in |
| 2 | All energy meters `unknown`: Riemann sensors had pinyin entity_ids; `utility_meter` sources expected `sensor.sonoff_<id>_energy` | Realign energy entity_ids to source via MCP registry |
| 3 | Riemann sensors never ticked: jitter rounded standby power to 1dp so state never changed | Round to 2dp, widen variance to ±15% |
| 4 | 13 registry orphans (UI-created, no YAML) | Purge via MCP |
| 5 | Mobile dashboard failed to load: missing mushroom/card-mod JS + broken `!include` | Materialize vendor JS, remap `/hacsfiles/`→`/local/community/`, add missing twin sensors |
| 6 | `ha_mcp` bridge selfcheck intermittently failed `light.turn_off`: read state once immediately, racing the MQTT command echo | Poll for state convergence (up to 5s) like the native selfcheck — 29/29 stable |
