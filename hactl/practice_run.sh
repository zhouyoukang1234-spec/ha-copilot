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

echo; echo "### practice run complete"
