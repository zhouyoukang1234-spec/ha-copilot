"""Offline check of the AssistAPI-style exposed-entity context formatter.

The live behaviour (``_get_exposed_entities`` on a running HA, gated on
``llm_context.assistant``) is proven against the running instance; this harness
pins the deterministic formatting so the area-grouped, token-efficient layout
can't regress. Run with the HA venv:

    python hactl/verify_entity_context.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from custom_components.ha_copilot.llm_api import _exposed_entities_prompt  # noqa: E402


def main() -> int:
    exposed = {
        "light.lr": {
            "names": "Ceiling",
            "domain": "light",
            "state": "on",
            "areas": "Living Room",
        },
        "switch.fan": {
            "names": "Fan",
            "domain": "switch",
            "state": "off",
            "areas": "Living Room",
        },
        "sensor.t": {"names": "Temp", "domain": "sensor", "state": "21"},
    }
    out = _exposed_entities_prompt(exposed)
    checks = {
        "header count": "(3 total)" in out,
        "area group": "[Living Room]" in out,
        "entity line": "- light.lr (Ceiling) = on" in out,
        "unassigned bucket": "[Unassigned]" in out,
        "areas sorted": out.index("[Living Room]") < out.index("[Unassigned]"),
    }
    for name, ok in checks.items():
        print(f"{'ok' if ok else 'FAIL'}: {name}")
    passed = all(checks.values())
    print("RESULT:", "PASS" if passed else "FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
