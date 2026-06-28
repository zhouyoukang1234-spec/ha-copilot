"""Pin the MCP tool-annotation classification for all tools.

Annotations (readOnlyHint / destructiveHint / idempotentHint) let MCP clients
flag destructive operations; this guards the classification against regressions.

    python hactl/verify_tool_annotations.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from custom_components.ha_copilot import tools  # noqa: E402


def main() -> int:
    names = [s["function"]["name"] for s in tools.TOOL_SPECS]
    problems: list[str] = []
    counts = {"read_only": 0, "write": 0, "destructive": 0}

    for name in names:
        a = tools.tool_annotations(name)
        if not {"title", "readOnlyHint", "destructiveHint", "idempotentHint"} <= a.keys():
            problems.append(f"{name}: missing annotation keys")
            continue
        if a["readOnlyHint"] and a["destructiveHint"]:
            problems.append(f"{name}: both readOnly and destructive")
        if a["readOnlyHint"]:
            counts["read_only"] += 1
        elif a["destructiveHint"]:
            counts["destructive"] += 1
        else:
            counts["write"] += 1
        # Invariants grounded in naming.
        if name.startswith(("list_", "get_")) and not a["readOnlyHint"]:
            problems.append(f"{name}: get/list must be read-only")
        if name.startswith("delete_") and not (a["destructiveHint"] and a["idempotentHint"]):
            problems.append(f"{name}: delete_* must be destructive + idempotent")
        if name in {"restart", "purge_recorder", "clear_statistics"} and not a["destructiveHint"]:
            problems.append(f"{name}: must be destructive")

    print(f"counts: {counts} total={len(names)}")
    for p in problems:
        print("FAIL:", p)
    print("RESULT:", "PASS" if not problems else "FAIL")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
