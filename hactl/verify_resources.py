"""Verify the Resource Hub tool layer (HACS / GitHub / blueprint discovery).

Two checks:

1. **Spec/annotation** — the five resource tools are registered in
   ``TOOL_SPECS`` and classified correctly (searches read-only, import a write).
2. **Live** (``--live``) — fetch the real HACS catalog and run the ranking to
   prove discovery works end-to-end against the network.

    python hactl/verify_resources.py            # spec/annotation only
    python hactl/verify_resources.py --live      # also hit HACS over the network

The live check loads ``resources.py`` in isolation (no Home Assistant runtime
required) and injects a plain aiohttp session, so it runs anywhere.
"""
import asyncio
import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RES_PATH = ROOT / "custom_components" / "ha_copilot" / "resources.py"

RESOURCE_TOOLS = {
    "search_community_resources": "read",
    "search_github": "read",
    "search_blueprints": "read",
    "recommend_resources": "read",
    "import_blueprint": "write",
}


def _load_resources_isolated():
    """Import resources.py without triggering the HA package __init__."""
    pkg = types.ModuleType("_hc_iso")
    pkg.__path__ = []  # type: ignore[attr-defined]
    sys.modules["_hc_iso"] = pkg
    const = types.ModuleType("_hc_iso.const")
    const.CONF_ALLOW_WRITE = "allow_write"  # type: ignore[attr-defined]
    const.CONF_ALLOW_RESTART = "allow_restart"  # type: ignore[attr-defined]
    sys.modules["_hc_iso.const"] = const
    spec = importlib.util.spec_from_file_location("_hc_iso.resources", RES_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_hc_iso.resources"] = mod
    spec.loader.exec_module(mod)
    return mod


def check_specs() -> list[str]:
    sys.path.insert(0, str(ROOT))
    from custom_components.ha_copilot import tools  # noqa: E402

    names = {s["function"]["name"] for s in tools.TOOL_SPECS}
    problems: list[str] = []
    for tool, kind in RESOURCE_TOOLS.items():
        if tool not in names:
            problems.append(f"{tool}: not registered in TOOL_SPECS")
            continue
        a = tools.tool_annotations(tool)
        if kind == "read" and not a["readOnlyHint"]:
            problems.append(f"{tool}: expected readOnlyHint=True")
        if kind == "write" and a["readOnlyHint"]:
            problems.append(f"{tool}: write tool must not be read-only")
    return problems


async def check_live() -> list[str]:
    import aiohttp

    m = _load_resources_isolated()
    problems: list[str] = []
    conn = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
    async with aiohttp.ClientSession(connector=conn) as sess:
        m.async_get_clientsession = lambda hass: sess

        class _H:
            pass

        r = await m.search_community_resources(_H(), "xiaomi", "integration", 5)
        if not r.get("ok") or r.get("total_catalog", 0) < 100:
            problems.append(f"community search weak: {r}")
        else:
            print(f"  community: {r['total_catalog']} integrations, "
                  f"top={r['results'][0]['full_name']}")
        b = _raw_url_checks(m)
        problems.extend(b)
    return problems


def _raw_url_checks(m) -> list[str]:
    problems: list[str] = []
    cases = {
        "https://github.com/a/b/blob/main/x.yaml":
            "https://raw.githubusercontent.com/a/b/main/x.yaml",
        "https://raw.githubusercontent.com/a/b/main/x.yaml":
            "https://raw.githubusercontent.com/a/b/main/x.yaml",
    }
    for src, want in cases.items():
        got = m._raw_url(src)
        if got != want:
            problems.append(f"_raw_url({src}) -> {got}, want {want}")
    return problems


def main() -> int:
    live = "--live" in sys.argv
    problems = check_specs()
    print(f"spec/annotation: {'PASS' if not problems else 'FAIL'}")
    if live:
        problems += asyncio.run(check_live())
    for p in problems:
        print("FAIL:", p)
    print("RESULT:", "PASS" if not problems else "FAIL")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
