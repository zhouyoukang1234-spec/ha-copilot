# HA-Copilot · Native MCP endpoint

This integration turns Home Assistant itself into an **MCP (Model Context
Protocol) server**. Any external agent — Devin, or any third-party platform
that speaks MCP — connects to a single endpoint served by HA and drives *all*
of Home Assistant. Deep native fusion **and** a public interface, one home.

```
            ┌──────────── Home Assistant process ────────────┐
 agent ───▶ │  /api/ha_copilot/mcp   (JSON-RPC over HTTP)     │
 (MCP)      │        │                                        │
            │        ├─ typed tools  ──▶ hass state/services/ │
            │        │                   registries/template/ │
            │        │                   recorder/config      │
            │        └─ ha_ws  ────────▶ ANY WebSocket command │
            │                            in-process (registries│
            │                            CRUD, Lovelace, auth, │
            │                            config entries, …)    │
            └────────────────────────────────────────────────┘
```

No network hop to HA, no separate sidecar: the tools execute **in-process**
against the live `hass` object, so they reach things the REST/WebSocket-from-
outside surface cannot, while remaining a standard, public MCP server.

## Endpoint

- URL: `POST http://<ha-host>:8123/api/ha_copilot/mcp`
- Transport: MCP Streamable-HTTP — JSON-RPC 2.0 request → `application/json`
  JSON-RPC response.
- Auth: **Home Assistant's own auth.** Send `Authorization: Bearer <token>`
  (a long-lived access token, or a logged-in session). Tools run as that user,
  so HA's permission model applies. `GET` on the same URL returns a small
  discovery document (server info + tool names).

## Methods

| JSON-RPC method | purpose |
|---|---|
| `initialize` | handshake; returns `protocolVersion`, `serverInfo`, capabilities |
| `tools/list` | advertise all tools with JSON-Schema `inputSchema` |
| `tools/call` | `{name, arguments}` → `{content, structuredContent, isError}` |
| `ping` | liveness |
| `notifications/*` | accepted, no response (202) |

## Tools (29)

**Typed, in-process (ergonomic):** `list_states` `get_state` `list_services`
`call_service` `render_template` `get_history` `check_config`
`registry_overview` `list_areas` `create_area` `rename_entity`
`assign_entity_area` `set_entity_enabled` `create_automation` `create_scene`
`create_script` `read_config_file` `write_config_file` `read_logs` `reload`.

**Convenience (ws-backed):** `list_dashboards` `list_floors` `list_labels`
`list_devices` `list_entities` `list_users` `list_config_entries`
`system_health`.

**Universal escape hatch:** `ha_ws` — run *any* Home Assistant WebSocket
command in-process, e.g.

```json
{"name": "ha_ws", "arguments": {
   "command_type": "config/area_registry/create",
   "payload": {"name": "Garage"}}}
```

This single tool reaches the entire frontend WebSocket surface (registries
create/update/delete, Lovelace dashboards, config entries, auth, …). 无为而无不为.

## Register as an MCP server (external client)

Streamable-HTTP MCP client config:

```json
{
  "mcpServers": {
    "home-assistant": {
      "type": "streamable-http",
      "url": "http://<ha-host>:8123/api/ha_copilot/mcp",
      "headers": { "Authorization": "Bearer <LONG_LIVED_TOKEN>" }
    }
  }
}
```

## Verify

`native_selfcheck.py` drives the live endpoint exactly like an external agent
(initialize → tools/list → tools/call), exercising every module read → write →
verify → clean up, and is idempotent:

```bash
HA_BASE_URL=http://localhost:8123 HA_TOKEN=$(cat ~/ha_llat.txt) \
  python3 -m custom_components.ha_copilot.native_selfcheck
# ==== 21/21 passed ====   (stable across repeated runs)
```
