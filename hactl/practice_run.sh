#!/usr/bin/env bash
# Full-chain practice run, driven entirely by the operator (no human, no LLM).
# Each step operates HA's bottom layer through hactl and verifies the result,
# closing the loop: act -> reload -> read back / observe state change.
#
# Usage:  bash practice_run.sh
# Requires: a minted token (see bootstrap_token.py) and a running HA at $HA_BASE.
#
# Entities are DISCOVERED from the live deployment (not hard-coded), so this runs
# end-to-end against any HA config — the mijia-twin (pinyin entity_ids) included.
set -euo pipefail
cd "$(dirname "$0")"
H() { echo; echo "### $*"; }
hactl() { python hactl.py "$@"; }

# --- discover real entities from the running system ---
mapfile -t LIGHTS < <(hactl states --domain light --brief \
  | python -c "import sys,json;[print(s['entity_id']) for s in json.load(sys.stdin)]")
if [ "${#LIGHTS[@]}" -lt 1 ]; then echo "no light entities found; aborting"; exit 1; fi
L0="${LIGHTS[0]}"
L1="${LIGHTS[1]:-$L0}"
L2="${LIGHTS[2]:-$L0}"
echo "discovered lights: $L0 | $L1 | $L2 (total ${#LIGHTS[@]})"

H "state plane: list lights"
hactl states --domain light --brief | head -20

H "registry plane: create an area"
hactl area-create "影音室"

H "config-editor plane: create an automation, then verify it is live + config valid"
hactl automation-create demo_dusk \
  "{\"alias\":\"演示日落开灯\",\"trigger\":[{\"platform\":\"sun\",\"event\":\"sunset\"}],\"action\":[{\"service\":\"light.turn_on\",\"target\":{\"entity_id\":\"$L0\"}}]}"
hactl automation-list | head -10
hactl check

H "scene: create -> activate -> observe the lights actually flip"
SCENE=$(hactl scene-create demo_movie \
  "{\"name\":\"演示影院\",\"entities\":{\"$L0\":\"off\",\"$L1\":\"on\",\"$L2\":\"off\"}}" \
  | python -c "import sys,json;print(json.load(sys.stdin)['entity_id'])")
echo "scene entity: $SCENE"
hactl call scene.turn_on --data "{\"entity_id\":\"$SCENE\"}"
sleep 1
hactl get "$L1"

H "script: create -> run -> observe lights off"
hactl script-create demo_all_off \
  "{\"alias\":\"演示全关\",\"sequence\":[{\"service\":\"light.turn_off\",\"target\":{\"entity_id\":[\"$L0\",\"$L1\",\"$L2\"]}}]}"
hactl call script.demo_all_off
sleep 1
hactl get "$L1"

H "registry write: rename an entity + assign it to the area, then read back"
hactl entity-update "$L0" --name "门廊射灯·演示" --area ying_yin_shi
hactl entity "$L0"

H "template plane: render against live state"
hactl template "{{ states.light | selectattr('state','eq','on') | list | count }}"

H "history plane: recent changes for one entity"
hactl history "$L0" --hours 1 | head -10

H "raw config plane: read the generated YAML the editor wrote"
hactl conf-get automations.yaml | head -10

# ---- deep-fusion round 1: introspection / registries / statistics / actions ----
H "introspection plane: HA core config (version, units, components)"
hactl tool get_core_config | python -c "import sys,json;d=json.load(sys.stdin);print('version',d['version'],'| components',d['components_count'],'| units',d['unit_system'])"

H "detailed entity registry (platform/area/device/labels)"
hactl tool list_entities --args "{\"domain\":\"light\"}" | python -c "import sys,json;d=json.load(sys.stdin);print('light entities:',d['count'])"

H "floor registry: create -> list -> delete"
hactl tool create_floor --args '{"name":"演练二楼","level":2}'
hactl tool list_floors | python -c "import sys,json;print('floors:',json.load(sys.stdin)['count'])"
hactl tool delete_floor --args '{"identifier":"演练二楼"}'

H "label registry: create -> assign to a light -> filter -> clear -> delete"
hactl tool create_label --args '{"name":"演练标签","color":"green"}'
hactl tool assign_entity_labels --args "{\"entity_id\":\"$L0\",\"labels\":[\"演练标签\"]}"
hactl tool list_entities --args '{"label":"yan_lian_biao_qian"}' | python -c "import sys,json;print('entities with label:',json.load(sys.stdin)['count'])"
hactl tool assign_entity_labels --args "{\"entity_id\":\"$L0\",\"labels\":[]}"
hactl tool delete_label --args '{"identifier":"演练标签"}'

H "statistics plane: list long-term stat ids + fetch one"
SID=$(hactl tool list_statistics | python -c "import sys,json;s=json.load(sys.stdin)['statistics'];print(s[0]['statistic_id'] if s else '')")
echo "first statistic_id: ${SID:-<none>}"
if [ -n "$SID" ]; then hactl tool get_statistics --args "{\"statistic_ids\":[\"$SID\"],\"hours\":72,\"period\":\"day\"}" | python -c "import sys,json;d=json.load(sys.stdin)['result'];print('points:',list(d.values())[0]['points'] if d else 0)"; fi

H "action runtime: run an ad-hoc script (turn light on, wait, off) without persisting"
hactl tool execute_script --args "{\"sequence\":[{\"service\":\"light.turn_on\",\"target\":{\"entity_id\":\"$L0\"}},{\"delay\":{\"milliseconds\":50}},{\"service\":\"light.turn_off\",\"target\":{\"entity_id\":\"$L0\"}}]}"
hactl get "$L0"

H "event bus: fire a custom event"
hactl tool fire_event --args '{"event_type":"ha_copilot_practice_event","event_data":{"src":"practice_run"}}'

# ---- deep-fusion round 2: logbook / users / categories / assist / todo / dashboards ----
H "logbook plane: humanised recent event timeline"
hactl tool get_logbook --args '{"hours":6}' | python -c "import sys,json;print('logbook entries:',json.load(sys.stdin)['count'])"

H "auth plane: list users"
hactl tool list_users | python -c "import sys,json;d=json.load(sys.stdin);print('users:',[u['name'] for u in d['users']])"

H "category registry: create -> list -> delete (scope=automation)"
hactl tool create_category --args '{"scope":"automation","name":"演练分类","icon":"mdi:tag"}'
hactl tool list_categories --args '{"scope":"automation"}' | python -c "import sys,json;print('categories:',json.load(sys.stdin)['count'])"
hactl tool delete_category --args '{"scope":"automation","identifier":"演练分类"}'

H "UI surface: list Lovelace dashboards"
hactl tool list_dashboards | python -c "import sys,json;d=json.load(sys.stdin);print('dashboards:',sorted({x['url_path'] for x in d['dashboards']}))"

H "assist NLU: process a natural-language command (controls a real light)"
LNAME=$(hactl tool list_entities --args '{"domain":"light"}' | python -c "import sys,json;print(json.load(sys.stdin)['entities'][0]['name'])")
hactl tool conversation_process --args "{\"text\":\"打开${LNAME}\",\"language\":\"zh-cn\"}" | python -c "import sys,json;r=json.load(sys.stdin)['response'];print('assist:',r['response_type'],'|',r['speech']['plain']['speech'])"

H "todo plane: add an item -> read back -> remove"
hactl tool add_todo_item --args '{"item":"演练待办项"}'
hactl tool list_todo_items | python -c "import sys,json;d=json.load(sys.stdin);print('todo items:',[i['summary'] for i in d['items']])"
hactl tool execute_script --args '{"sequence":[{"service":"todo.remove_item","target":{"entity_id":"todo.shopping_list"},"data":{"item":"演练待办项"}}]}'

# ---- deep-fusion round 3: live events / tags / system_health / blueprint ----
H "self-diagnostic: aggregate system_health across integrations"
hactl tool get_system_health | python -c "import sys,json;d=json.load(sys.stdin);print('health domains:',d['count'],'| core:',d['health'].get('homeassistant',{}).get('version'))"

H "tag registry: create -> list -> delete (NFC/RFID/QR)"
hactl tool create_tag --args '{"name":"演练标签"}'
hactl tool list_tags | python -c "import sys,json;d=json.load(sys.stdin);print('tags:',[t['name'] for t in d['tags']])"
hactl tool delete_tag --args '{"identifier":"演练标签"}'

H "blueprint deep-dive: full inputs/schema of motion_light"
hactl tool get_blueprint --args '{"path":"homeassistant/motion_light.yaml"}' | python -c "import sys,json;d=json.load(sys.stdin);print('blueprint:',d['name'],'| inputs:',list(d['inputs'].keys()))"

H "live event bus: wait_for_event captures a concurrently-fired event"
( sleep 2; hactl tool fire_event --args '{"event_type":"ha_copilot_practice_live","event_data":{"src":"practice"}}' >/dev/null ) &
hactl tool wait_for_event --args '{"event_type":"ha_copilot_practice_live","timeout":8}' | python -c "import sys,json;d=json.load(sys.stdin);print('captured:',not d['timed_out'],'| data:',d.get('data'))"

# ---- deep-fusion round 4: deep introspection (service/area/entity/config_entry) + wait_for_template ----
H "service schema: exact call shape of light.turn_on (fields + target)"
hactl tool describe_service --args '{"domain":"light","service":"turn_on"}' | python -c "import sys,json;d=json.load(sys.stdin);print('fields:',list(d['fields'].keys())[:6],'| has target:',bool(d.get('target')))"

H "area relationship graph: resolve first area -> devices + effective entities"
AID=$(hactl tool list_areas | python -c "import sys,json;a=json.load(sys.stdin)['areas'];print(a[0].get('area_id') or a[0].get('id'))")
hactl tool describe_area --args "{\"identifier\":\"$AID\"}" | python -c "import sys,json;d=json.load(sys.stdin);print('area:',d['name'],'| devices:',d['device_count'],'| entities:',d['entity_count'])"

H "entity registry deep-dive: unique_id/platform/owner of first light"
EID=$(hactl tool list_entities --args '{"domain":"light"}' | python -c "import sys,json;print(json.load(sys.stdin)['entities'][0]['entity_id'])")
hactl tool get_entity_registry_entry --args "{\"entity_id\":\"$EID\"}" | python -c "import sys,json;d=json.load(sys.stdin);print('unique_id:',d['unique_id'],'| platform:',d['platform'])"

H "config entry detail: first integration's load state + options"
DOM=$(hactl tool list_config_entries | python -c "import sys,json;print(json.load(sys.stdin)['entries'][0]['domain'])")
hactl tool get_config_entry --args "{\"identifier\":\"$DOM\"}" | python -c "import sys,json;d=json.load(sys.stdin);print('entry:',d['domain'],'| state:',d['state'],'| version:',d['version'])"

H "template wait: turn light off, then wait_for_template until it is on (toggle concurrently)"
hactl tool call_service --args "{\"domain\":\"light\",\"service\":\"turn_off\",\"data\":{\"entity_id\":\"$EID\"}}" >/dev/null
( sleep 2; hactl tool call_service --args "{\"domain\":\"light\",\"service\":\"turn_on\",\"data\":{\"entity_id\":\"$EID\"}}" >/dev/null ) &
hactl tool wait_for_template --args "{\"template\":\"{{ is_state('$EID','on') }}\",\"timeout\":8}" | python -c "import sys,json;d=json.load(sys.stdin);print('matched:',d['matched'],'| waited:',d.get('waited'))"

echo; echo "### practice run complete"
