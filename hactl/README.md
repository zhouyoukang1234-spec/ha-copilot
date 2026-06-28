# hactl — bottom-layer Home Assistant control for an AI operator

`hactl` is infrastructure built **for the AI to operate Home Assistant directly**,
not a chat box for a weak model to talk through. The AI (the strong operator) drives
HA's bottom layer full-chain through one scriptable command line: every command takes
plain arguments and prints a single JSON object, so results are trivially readable and
chainable. This is the "Cursor-for-HA" idea anchored correctly — the intelligence is the
operator; `hactl` is the editor + tools it works through.

## Why this shape

- **Operator-first, not chat-first.** No intermediary model in the loop; the AI issues
  exact operations and reads exact results.
- **Official bottom layer.** REST for the state plane, WebSocket for the registries,
  the config-editor API for automations/scenes/scripts (the same path the HA UI uses),
  and direct YAML file access for raw config.
- **Closed-loop by construction.** Write → reload → read back / observe the state change.

## Setup

```bash
pip install requests websockets pyyaml
python bootstrap_token.py     # mints a long-lived token into ../.ha_token (gitignored)
```

Token resolution: `$HA_TOKEN`, else `../.ha_token`.
Endpoint: `$HA_BASE` (default `http://localhost:8123`).
Raw-config access (`conf-get`/`conf-set`) is routed through the `ha_copilot`
tool API, so it works against any deployment (Docker, bare metal, WSL) with no
extra configuration.

## Command surface

| plane | commands |
|---|---|
| state | `states [--domain D] [--brief]`, `get <id>`, `call <domain.service> [--data JSON] [--response]`, `template <jinja>`, `history <id> [--hours N]`, `services [--domain D]`, `error-log`, `config` |
| validate | `check`, `reload [--domain D]` |
| registry | `areas`, `area-create <name>`, `entities [--domain D]`, `entity <id>`, `entity-update <id> [--name] [--area] [--new-id] [--disabled true/false]`, `devices`, `device-update <id> [--area] [--name]` |
| config-editor | `automation-list`, `automation-create <id> <json>`, `scene-create <id> <json>`, `script-create <id> <json>` |
| raw config | `conf-get <path>`, `conf-set <path> <content|->` |

## Examples

```bash
# read
python hactl.py states --domain light --brief
python hactl.py template "{{ states.light | selectattr('state','eq','on') | list | count }}"

# act on the bottom layer
python hactl.py area-create "影音室"
python hactl.py entity-update input_boolean.porch_light --name "门廊射灯" --area ying_yin_shi
python hactl.py scene-create movie '{"name":"影院","entities":{"light.living_room":"off","light.porch":"on"}}'
python hactl.py call scene.turn_on --data '{"entity_id":"scene.ying_yuan"}'

# validate
python hactl.py check
```

## Full-chain practice run

`practice_run.sh` exercises every plane and verifies each step closes the loop:

```bash
bash practice_run.sh
```
