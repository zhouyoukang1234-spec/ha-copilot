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

# ---- deep-fusion round 5: device graph / stat metadata / condition eval / zones / automation trace ----
H "device graph: deep introspection of first device + its entities"
DID=$(hactl tool list_devices | python -c "import sys,json;d=json.load(sys.stdin)['devices'];print(d[0]['id'] if d else '')")
if [ -n "$DID" ]; then hactl tool get_device --args "{\"identifier\":\"$DID\"}" | python -c "import sys,json;d=json.load(sys.stdin);print('device:',d['name'],'| model:',d.get('model'),'| entities:',d['entity_count'])"; fi

H "recorder statistic metadata: source/unit/has_mean/has_sum"
hactl tool get_statistic_metadata | python -c "import sys,json;d=json.load(sys.stdin);print('stats:',d['count'])"

H "condition engine: evaluate state + template + numeric conditions against live state"
hactl tool call_service --args "{\"domain\":\"light\",\"service\":\"turn_on\",\"data\":{\"entity_id\":\"$EID\"}}" >/dev/null
hactl tool evaluate_condition --args "{\"condition\":{\"condition\":\"state\",\"entity_id\":\"$EID\",\"state\":\"on\"}}" | python -c "import sys,json;print('state cond ->',json.load(sys.stdin)['result'])"
hactl tool evaluate_condition --args '{"condition":"{{ 5 > 3 }}"}' | python -c "import sys,json;print('template cond ->',json.load(sys.stdin)['result'])"

H "presence/geofence: list zones + persons inside"
hactl tool list_zones | python -c "import sys,json;d=json.load(sys.stdin);print('zones:',[z['name'] for z in d['zones']])"

H "automation debug: most recent execution trace of a known automation"
hactl tool get_automation_trace --args '{"identifier":"download_bing_wallpaper_daily"}' | python -c "import sys,json;d=json.load(sys.stdin);l=d.get('latest',{});print('trace count:',d['count'],'| script_execution:',l.get('script_execution'),'| state:',l.get('state'))"

# ---- deep-fusion round 6: system log / manifest / recorder / loaded integrations / template vars / service response ----
H "system logs: recent captured records (the Logs panel surface)"
hactl tool get_system_log | python -c "import sys,json;d=json.load(sys.stdin);print('log records total:',d.get('total'),'| shown:',d['count'])"

H "integration manifest: what the 'light' integration is made of"
hactl tool get_integration_manifest --args '{"domain":"light"}' | python -c "import sys,json;d=json.load(sys.stdin);print('name:',d['name'],'| built_in:',d['is_built_in'],'| quality:',d.get('quality_scale'))"

H "recorder health: recording + write backlog"
hactl tool get_recorder_info | python -c "import sys,json;d=json.load(sys.stdin);print('recording:',d['recording'],'| backlog:',d['backlog'])"

H "loaded integrations: how many components are live in this instance"
hactl tool get_loaded_integrations | python -c "import sys,json;d=json.load(sys.stdin);print('loaded components:',d['count'])"

H "template with variables: emulate an automation render context"
hactl tool render_template --args '{"template":"{{ trigger.to_state }} / {{ myvar * 2 }}","variables":{"trigger":{"to_state":"on"},"myvar":21}}' | python -c "import sys,json;print('rendered:',json.load(sys.stdin)['result'])"

H "service response: weather.get_forecasts returns a payload (not just ok)"
WID=$(hactl tool list_states --args '{"domain":"weather"}' | python -c "import sys,json;e=json.load(sys.stdin)['entities'];print(e[0]['entity_id'] if e else '')")
if [ -n "$WID" ]; then hactl tool call_service_response --args "{\"domain\":\"weather\",\"service\":\"get_forecasts\",\"data\":{\"entity_id\":\"$WID\",\"type\":\"daily\"}}" | python -c "import sys,json;d=json.load(sys.stdin);print('ok:',d.get('ok'),'| response entities:',list((d.get('response') or {}).keys()))"; fi

# ---- deep-fusion round 7: set_state / automation config + validate / config flows / import statistics ----
H "set_state: seed a virtual sensor value, then read it back via a template"
hactl tool set_state --args '{"entity_id":"sensor.copilot_practice_probe","state":"42","attributes":{"unit_of_measurement":"W"}}' >/dev/null
hactl tool render_template --args '{"template":"{{ states(\"sensor.copilot_practice_probe\") }}"}' | python -c "import sys,json;print('virtual sensor renders:',json.load(sys.stdin)['result'])"

H "validate_automation_config: check an automation config against HA's schema before saving"
hactl tool validate_automation_config --args '{"config":{"alias":"practice ok","triggers":[{"trigger":"state","entity_id":"sun.sun"}],"actions":[{"action":"homeassistant.update_entity","target":{"entity_id":"sun.sun"}}]}}' | python -c "import sys,json;d=json.load(sys.stdin);print('valid:',d['valid'],'| triggers:',d.get('trigger_count'),'| actions:',d.get('action_count'))"

H "get_automation_config: the definition behind an automation entity"
hactl tool get_automation_config --args '{"identifier":"download_bing_wallpaper_daily"}' | python -c "import sys,json;d=json.load(sys.stdin);c=d.get('config',{});print('found:',d['found'],'| alias:',c.get('alias'),'| mode:',c.get('mode'))"

H "list_config_flows: how many integrations support a UI setup flow"
hactl tool list_config_flows | python -c "import sys,json;d=json.load(sys.stdin);print('config-flow integrations:',d['supported_count'],'| in-progress:',d['in_progress_count'])"

H "import_statistics: backfill external long-term statistics (energy dashboard)"
NOW=$(python -c "import datetime;n=datetime.datetime.now(datetime.timezone.utc).replace(minute=0,second=0,microsecond=0);import json;print(json.dumps([{'start':(n-datetime.timedelta(hours=2)).isoformat(),'sum':10,'state':10},{'start':(n-datetime.timedelta(hours=1)).isoformat(),'sum':25,'state':15}]))")
hactl tool import_statistics --args "{\"statistic_id\":\"ha_copilot:practice_energy\",\"statistics\":$NOW,\"unit\":\"kWh\",\"name\":\"Copilot Practice Energy\",\"has_sum\":true}" | python -c "import sys,json;d=json.load(sys.stdin);print('imported points:',d.get('imported'),'| external:',d.get('external'))"

# ---- deep-fusion round 8: script/scene config / device automations / stats period + clear ----
H "get_script_config: the sequence behind a script entity (by object_id)"
hactl tool get_script_config --args '{"identifier":"home_mode"}' | python -c "import sys,json;d=json.load(sys.stdin);c=d.get('config',{});print('found:',d['found'],'| alias:',c.get('alias'),'| steps:',len(c.get('sequence',[])))"

H "get_scene_config: the entity states a scene restores"
hactl tool get_scene_config --args '{"identifier":"回家模式"}' | python -c "import sys,json;d=json.load(sys.stdin);print('found:',d['found'],'| entities in scene:',d.get('entity_count'))"

H "get_device_automations: device-based trigger capabilities of the first device"
DID=$(hactl tool list_devices | python -c "import sys,json;d=json.load(sys.stdin).get('devices',[]);print(d[0]['id'] if d else '')")
if [ -n "$DID" ]; then hactl tool get_device_automations --args "{\"device_id\":\"$DID\",\"type\":\"trigger\"}" | python -c "import sys,json;d=json.load(sys.stdin);print('device triggers available:',d.get('count'))"; fi

H "import + get_statistics_during_period + clear: a clean long-term-stats round trip"
NOW8=$(python -c "import datetime,json;n=datetime.datetime.now(datetime.timezone.utc).replace(minute=0,second=0,microsecond=0);print(json.dumps({'pts':[{'start':(n-datetime.timedelta(hours=3)).isoformat(),'sum':5,'state':5},{'start':(n-datetime.timedelta(hours=2)).isoformat(),'sum':9,'state':4}],'from':(n-datetime.timedelta(hours=4)).isoformat(),'to':n.isoformat()}))")
PTS=$(echo "$NOW8" | python -c "import sys,json;print(json.dumps(json.load(sys.stdin)['pts']))")
FROM=$(echo "$NOW8" | python -c "import sys,json;print(json.load(sys.stdin)['from'])")
TO=$(echo "$NOW8" | python -c "import sys,json;print(json.load(sys.stdin)['to'])")
hactl tool import_statistics --args "{\"statistic_id\":\"ha_copilot:practice_r8\",\"statistics\":$PTS,\"unit\":\"kWh\",\"has_sum\":true}" >/dev/null
sleep 3
hactl tool get_statistics_during_period --args "{\"statistic_ids\":[\"ha_copilot:practice_r8\"],\"start\":\"$FROM\",\"end\":\"$TO\",\"period\":\"hour\"}" | python -c "import sys,json;d=json.load(sys.stdin);r=d['result'].get('ha_copilot:practice_r8',{});print('period rows:',r.get('points'))"
hactl tool clear_statistics --args '{"statistic_ids":["ha_copilot:practice_r8"]}' | python -c "import sys,json;d=json.load(sys.stdin);print('cleared:',d.get('cleared'))"

echo; echo "### practice run complete"
