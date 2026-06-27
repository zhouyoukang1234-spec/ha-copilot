# HA-MCP — plug an AI agent into all of Home Assistant

`ha_mcp` is a [Model Context Protocol](https://modelcontextprotocol.io) server
that exposes the **entire user-operable surface** of a Home Assistant instance
to an external agent. It is the "Cursor for Home Assistant" plumbing: a strong
agent on one side, a complete-but-thin control layer over HA on the other.

Unlike an embedded chat assistant, HA-MCP makes *the agent itself* the operator
— it does everything a person can do in the HA UI, over the same REST +
WebSocket APIs the frontend uses.

## What it can drive (44 tools)

| Area | Tools |
|---|---|
| Discovery | `ha_overview`, `get_config`, `check_config` |
| States & services | `list_states`, `get_state`, `set_state`, `list_services`, `call_service`, `render_template` |
| History & logs | `get_history`, `get_logbook`, `get_error_log` |
| Registries | `list_areas`/`create_area`/`update_area`/`delete_area`, `list_floors`/`create_floor`, `list_devices`/`update_device`, `list_entities`/`get_entity`/`update_entity`/`remove_entity`, `list_labels`/`create_label` |
| Automations / scenes / scripts | `list_automations`/`get_automation`/`save_automation`/`delete_automation`, `get_scene`/`save_scene`, `get_script`/`save_script` |
| Helpers | `list_helpers`, `create_helper` |
| Dashboards | `list_dashboards`, `get_dashboard`, `save_dashboard` |
| Users / integrations / system | `list_users`, `list_config_entries`, `system_health` |
| Universal escape hatches | `ha_rest`, `ha_ws` (reach anything not yet wrapped) |

## Configure

```bash
pip install -r ha_mcp/requirements.txt
export HA_BASE_URL=http://localhost:8123
export HA_TOKEN=$(cat ha_llat.txt)   # a long-lived token (see mint_token.py)
```

Mint a durable token once (after onboarding):
```bash
python -m ha_mcp.mint_token deploy/ha_token.txt   # writes ha_llat.txt
```

## Run

- **stdio** (default — for a client that launches the server as a subprocess):
  ```bash
  python -m ha_mcp.server
  ```
- **streamable HTTP** (a remote endpoint other agents register):
  ```bash
  HA_MCP_TRANSPORT=streamable-http python -m ha_mcp.server   # serves /mcp
  ```

### Register with an MCP client

stdio (Claude Desktop / Cursor / Devin-style `mcpServers`):
```json
{
  "mcpServers": {
    "ha-mcp": {
      "command": "python",
      "args": ["-m", "ha_mcp.server"],
      "env": { "HA_BASE_URL": "http://localhost:8123", "HA_TOKEN": "<long-lived-token>" }
    }
  }
}
```

## Verify — drive every module end to end

```bash
HA_BASE_URL=http://localhost:8123 HA_TOKEN=$(cat ha_llat.txt) python -m ha_mcp.selfcheck
```
`selfcheck` speaks the real MCP protocol to the server and exercises every
module against a live HA — reading, writing, verifying the closed loop and
cleaning up after itself. Expect `29/29 passed`; it is idempotent across reruns.
