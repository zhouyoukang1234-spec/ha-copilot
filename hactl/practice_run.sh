#!/usr/bin/env bash
# Full-chain practice run, driven entirely by the operator (no human, no LLM).
# Each step operates HA's bottom layer through hactl and verifies the result,
# closing the loop: act -> reload -> read back / observe state change.
#
# Usage:  bash practice_run.sh
# Requires: a minted token (see bootstrap_token.py) and a running HA at $HA_BASE.
set -euo pipefail
cd "$(dirname "$0")"
H() { echo; echo "### $*"; }
hactl() { python hactl.py "$@"; }

H "state plane: list lights"
hactl states --domain light --brief

H "registry plane: create an area"
hactl area-create "影音室"

H "config-editor plane: create an automation, then verify it is live + config valid"
hactl automation-create demo_dusk \
  '{"alias":"演示日落开灯","trigger":[{"platform":"sun","event":"sunset"}],"action":[{"service":"light.turn_on","target":{"entity_id":"light.living_room"}}]}'
hactl automation-list
hactl check

H "scene: create -> activate -> observe the lights actually flip"
SCENE=$(hactl scene-create demo_movie \
  '{"name":"演示影院","entities":{"light.living_room":"off","light.porch":"on","light.bedroom":"off"}}' \
  | python -c "import sys,json;print(json.load(sys.stdin)['entity_id'])")
echo "scene entity: $SCENE"
hactl call scene.turn_on --data "{\"entity_id\":\"$SCENE\"}"
sleep 1
hactl states --domain light --brief

H "script: create -> run -> observe all lights off"
hactl script-create demo_all_off \
  '{"alias":"演示全关","sequence":[{"service":"light.turn_off","target":{"entity_id":["light.living_room","light.bedroom","light.porch"]}}]}'
hactl call script.demo_all_off
sleep 1
hactl states --domain light --brief

H "registry write: rename an entity + assign it to the area, then read back"
hactl entity-update input_boolean.porch_light --name "门廊射灯" --area ying_yin_shi
hactl entity input_boolean.porch_light

H "template plane: render against live state"
hactl template "{{ states.light | selectattr('state','eq','on') | list | count }}"

H "history plane: recent changes for one entity"
hactl history light.living_room --hours 1

H "raw config plane: read the generated YAML the editor wrote"
hactl conf-get automations.yaml

echo; echo "### practice run complete"
