"""Resource Hub: discover and match community smart-home resources.

This module is the operator's window onto the wider Home Assistant / smart-home
ecosystem. It turns "the whole network of HA resources" into a set of
deterministic tools — no model, no inference — that:

* search the **HACS** catalog (custom integrations, Lovelace/frontend cards,
  themes, AppDaemon apps, python_scripts),
* search **GitHub** for HA-related repositories, templates and examples,
* search community **blueprints** (automation/script templates),
* **recommend** resources matched to the devices/integrations that actually
  exist in the running Home Assistant — so even a non-expert gets the right
  integration or card for their hardware without knowing what to look for,
* **import a blueprint** by URL straight into the running HA config.

All network access is read-only fetching over HA's shared aiohttp session, with
short timeouts and graceful errors. The only writing operation is
``import_blueprint``, which is gated by ``allow_write`` and confined to the HA
config directory (same guarantees as the rest of the tool layer).
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import yaml

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import slugify as _slugify

from .const import CONF_ALLOW_WRITE


class _BlueprintLoader(yaml.SafeLoader):
    """SafeLoader that tolerates HA's custom tags (``!input``, ``!include`` …).

    Blueprints universally use ``!input`` (and configs use ``!include`` /
    ``!secret``), which a plain ``safe_load`` rejects. We only need to inspect
    the document (confirm it is a blueprint, read name/domain) — the raw text is
    written verbatim, so HA's own loader resolves the tags at use time.
    """


def _construct_undefined(loader: yaml.Loader, tag_suffix: str, node: yaml.Node):
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_scalar(node)


_BlueprintLoader.add_multi_constructor("!", _construct_undefined)

# HACS publishes a machine-readable catalog per category. These are static JSON
# blobs (no auth), keyed by repository id.
HACS_DATA_BASE = "https://data-v2.hacs.xyz"
HACS_CATEGORIES = {
    "integration": "integration",
    "plugin": "plugin",            # Lovelace / frontend cards
    "frontend": "plugin",          # alias
    "card": "plugin",              # alias
    "theme": "theme",
    "appdaemon": "appdaemon",
    "python_script": "python_script",
}
_ALL_CATEGORIES = ("integration", "plugin", "theme")

GITHUB_SEARCH = "https://api.github.com/search/repositories"

_USER_AGENT = "ha-copilot-resource-hub"
_CACHE_TTL = 600.0  # seconds; HACS feeds change slowly
_cache: dict[str, tuple[float, Any]] = {}


def _safe_path(hass: HomeAssistant, rel_path: str) -> str:
    """Resolve a config-relative path, refusing to escape the config dir."""
    base = os.path.realpath(hass.config.config_dir)
    target = os.path.realpath(os.path.join(base, rel_path))
    if target != base and not target.startswith(base + os.sep):
        raise ValueError(f"path '{rel_path}' escapes the config directory")
    return target


def _request_headers(url: str) -> dict[str, str]:
    """Base headers, plus GitHub auth when a token is available.

    Unauthenticated GitHub search is limited to ~10 requests/minute, which a
    live deployment hits quickly. If the operator exports a token
    (``GITHUB_TOKEN`` / ``GH_TOKEN``) it is attached to api.github.com calls,
    lifting the limit to 30/min (search) / 5000/hr (core). No token is ever
    stored by ha_copilot; this only reads the process environment.
    """
    headers = {"User-Agent": _USER_AGENT}
    if "api.github.com" in url:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers


async def _fetch_json(hass: HomeAssistant, url: str, timeout: float = 20.0) -> Any:
    session = async_get_clientsession(hass)
    async with session.get(
        url, headers=_request_headers(url), timeout=timeout
    ) as resp:
        if resp.status == 403 and "api.github.com" in url:
            remaining = resp.headers.get("X-RateLimit-Remaining")
            if remaining == "0":
                raise RuntimeError(
                    "GitHub API rate limit exceeded. Export a GITHUB_TOKEN "
                    "(or GH_TOKEN) for the HA process to raise the limit."
                )
        resp.raise_for_status()
        return await resp.json(content_type=None)


async def _fetch_text(hass: HomeAssistant, url: str, timeout: float = 20.0) -> str:
    session = async_get_clientsession(hass)
    async with session.get(
        url, headers={"User-Agent": _USER_AGENT}, timeout=timeout
    ) as resp:
        resp.raise_for_status()
        return await resp.text()


async def _hacs_catalog(hass: HomeAssistant, category: str) -> list[dict[str, Any]]:
    """Return the normalized HACS catalog for one category (cached)."""
    cat = HACS_CATEGORIES.get(category, category)
    key = f"hacs:{cat}"
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    raw = await _fetch_json(hass, f"{HACS_DATA_BASE}/{cat}/data.json")
    items: list[dict[str, Any]] = []
    for repo_id, v in (raw or {}).items():
        if not isinstance(v, dict):
            continue
        full_name = v.get("full_name") or ""
        name = (
            v.get("manifest_name")
            or (v.get("manifest") or {}).get("name")
            or full_name.split("/")[-1]
        )
        items.append(
            {
                "id": repo_id,
                "name": name,
                "full_name": full_name,
                "category": cat,
                "domain": v.get("domain"),
                "description": v.get("description") or "",
                "stars": v.get("stargazers_count") or 0,
                "topics": v.get("topics") or [],
                "last_version": v.get("last_version"),
                "url": f"https://github.com/{full_name}" if full_name else None,
            }
        )
    _cache[key] = (now, items)
    return items


def _score(item: dict[str, Any], terms: list[str]) -> int:
    """Keyword relevance: weighted hits across name/domain/desc/topics."""
    name = (item.get("name") or "").lower()
    full = (item.get("full_name") or "").lower()
    domain = (item.get("domain") or "").lower()
    desc = (item.get("description") or "").lower()
    topics = " ".join(item.get("topics") or []).lower()
    score = 0
    for t in terms:
        if not t:
            continue
        if t == domain or t == name:
            score += 6
        if t in name or t in full:
            score += 4
        if t in topics:
            score += 3
        if t in desc:
            score += 1
    return score


async def search_community_resources(
    hass: HomeAssistant,
    query: str = "",
    category: str = "all",
    limit: int = 20,
) -> dict[str, Any]:
    """Search the HACS catalog (integrations / cards / themes) by keyword."""
    cats = (
        list(_ALL_CATEGORIES)
        if category in ("all", "")
        else [HACS_CATEGORIES.get(category, category)]
    )
    terms = [w for w in (query or "").lower().split() if w]
    catalogs = await asyncio.gather(
        *[_hacs_catalog(hass, c) for c in cats], return_exceptions=True
    )
    pool: list[dict[str, Any]] = []
    errors: list[str] = []
    for c, res in zip(cats, catalogs):
        if isinstance(res, Exception):
            errors.append(f"{c}: {type(res).__name__}: {res}")
        else:
            pool.extend(res)
    if terms:
        ranked = [(it, _score(it, terms)) for it in pool]
        ranked = [(it, s) for it, s in ranked if s > 0]
        ranked.sort(key=lambda p: (p[1], p[0]["stars"]), reverse=True)
        results = [it for it, _ in ranked]
    else:
        results = sorted(pool, key=lambda it: it["stars"], reverse=True)
    out = {
        "ok": True,
        "query": query,
        "categories": cats,
        "total_catalog": len(pool),
        "count": min(len(results), max(1, limit)),
        "results": results[: max(1, limit)],
    }
    if errors:
        out["partial_errors"] = errors
    return out


async def search_github(
    hass: HomeAssistant,
    query: str,
    sort: str = "stars",
    limit: int = 15,
) -> dict[str, Any]:
    """Search GitHub repositories for HA-related projects/templates/examples.

    ``query`` is appended to a Home-Assistant context so brand/device terms find
    the right ecosystem repos. Uses GitHub's public search API (unauthenticated;
    low rate limit, so use sparingly).
    """
    q = query.strip()
    if "home" not in q.lower() and "assistant" not in q.lower():
        q = f"{q} home assistant"
    params = (
        f"?q={_url_quote(q)}"
        f"&sort={_url_quote(sort)}&order=desc&per_page={max(1, min(limit, 30))}"
    )
    try:
        data = await _fetch_json(hass, GITHUB_SEARCH + params)
    except Exception as err:  # noqa: BLE001 - surface fetch/ratelimit errors
        return {"error": f"{type(err).__name__}: {err}"}
    items = []
    for r in (data or {}).get("items", [])[: max(1, limit)]:
        items.append(
            {
                "full_name": r.get("full_name"),
                "description": r.get("description") or "",
                "stars": r.get("stargazers_count") or 0,
                "topics": r.get("topics") or [],
                "url": r.get("html_url"),
                "updated": r.get("pushed_at"),
                "license": (r.get("license") or {}).get("spdx_id"),
            }
        )
    return {
        "ok": True,
        "query": q,
        "total": (data or {}).get("total_count", len(items)),
        "count": len(items),
        "results": items,
    }


async def search_blueprints(
    hass: HomeAssistant,
    query: str = "",
    limit: int = 15,
) -> dict[str, Any]:
    """Find community blueprint repositories (automation/script templates).

    Matches blueprint repositories/collections by free-text so an operator can
    locate ready-made automations to import via ``import_blueprint``.

    GitHub repo search ANDs every word, so a natural multi-word phrase plus the
    "home assistant blueprint" context easily over-constrains to zero hits. We
    therefore try progressively-relaxed queries and return the first that hits,
    so a non-expert phrase like "low battery notification" still surfaces
    results instead of an empty list.
    """
    base = query.strip()
    # Most specific -> least specific, but always floored at a home-assistant
    # context: dropping it entirely returns unrelated repos (e.g. random
    # "reminder" projects), so we never relax below that.
    candidates = [
        f"{base} home assistant blueprint",
        f"{base} home-assistant blueprint",
        f"{base} home-assistant",
    ]
    seen: set[str] = set()
    ladder = [c.strip() for c in candidates if c.strip() and not (c.strip() in seen or seen.add(c.strip()))]
    if not ladder:
        ladder = ["home assistant blueprint"]
    per_page = max(1, min(limit, 30))
    data: dict[str, Any] = {}
    q = ladder[0]
    try:
        for cand in ladder:
            q = cand
            params = f"?q={_url_quote(q)}&sort=stars&order=desc&per_page={per_page}"
            data = await _fetch_json(hass, GITHUB_SEARCH + params)
            if (data or {}).get("total_count", 0):
                break
    except Exception as err:  # noqa: BLE001 - surface fetch/ratelimit errors
        return {"error": f"{type(err).__name__}: {err}"}
    items = []
    for r in (data or {}).get("items", [])[: max(1, limit)]:
        items.append(
            {
                "full_name": r.get("full_name"),
                "description": r.get("description") or "",
                "stars": r.get("stargazers_count") or 0,
                "url": r.get("html_url"),
                "import_hint": (
                    "Call list_repo_blueprints with this full_name to get "
                    "ready-to-import raw .yaml URLs for import_blueprint."
                ),
            }
        )
    return {
        "ok": True,
        "query": q,
        "total": (data or {}).get("total_count", len(items)),
        "count": len(items),
        "results": items,
    }


_GITHUB_API = "https://api.github.com"
_RAW_BASE = "https://raw.githubusercontent.com"


async def list_repo_blueprints(
    hass: HomeAssistant,
    repo: str,
    limit: int = 30,
) -> dict[str, Any]:
    """List directly-importable blueprint .yaml URLs inside a GitHub repo.

    Closes the search -> import loop: ``search_blueprints`` finds repos, this
    resolves a repo (``owner/name`` or a GitHub URL) to the raw URLs of the
    blueprint files it contains, each ready to hand straight to
    ``import_blueprint``.

    Two-tier detection: first the canonical HA layout (a ``.yaml``/``.yml``
    under a ``blueprints/`` directory) with no extra fetches. If that finds
    nothing, fall back to **content-sniffing** the repo's shallow ``.yaml``
    files (a great many community blueprints are a single file at repo root)
    for a top-level ``blueprint:`` mapping — the definitive blueprint signature
    — which recovers those without misfiring on configs/CI/issue-templates.
    ``import_blueprint`` still validates on import.
    """
    slug = repo.strip()
    for pre in (f"{_RAW_BASE}/", "https://github.com/", "github.com/"):
        if slug.startswith(pre):
            slug = slug[len(pre):]
            break
    parts = [p for p in slug.split("/") if p]
    if len(parts) < 2:
        return {"error": f"expected owner/repo, got {repo!r}"}
    owner, name = parts[0], parts[1]
    try:
        meta = await _fetch_json(hass, f"{_GITHUB_API}/repos/{owner}/{name}")
        branch = meta.get("default_branch") or "main"
        tree = await _fetch_json(
            hass, f"{_GITHUB_API}/repos/{owner}/{name}/git/trees/{branch}?recursive=1"
        )
    except Exception as err:  # noqa: BLE001 - surface fetch/ratelimit errors
        return {"error": f"{type(err).__name__}: {err}"}
    cap = max(1, limit)
    yaml_paths = [
        node.get("path", "")
        for node in (tree or {}).get("tree", [])
        if node.get("type") == "blob"
        and node.get("path", "").lower().endswith((".yaml", ".yml"))
    ]

    def _raw(path: str) -> str:
        return f"{_RAW_BASE}/{owner}/{name}/{branch}/{path}"

    # Tier 1: canonical blueprints/ directory (path-only, no fetch).
    results = [
        {"path": p, "raw_url": _raw(p)}
        for p in yaml_paths
        if "blueprints" in p.lower().split("/")[:-1]
    ][:cap]
    detection = "blueprints-dir"

    # Tier 2: content-sniff shallow yaml for a top-level ``blueprint:`` key.
    if not results:
        detection = "content-sniff"
        # Shallow files only (depth <= 1) to bound fetches and skip vendored/CI.
        shallow = [p for p in yaml_paths if p.count("/") <= 1][:8]
        for p in shallow:
            try:
                text = await _fetch_text(hass, _raw(p))
                doc = yaml.load(text, Loader=_BlueprintLoader)  # noqa: S506
            except Exception:  # noqa: BLE001 - skip unfetchable/unparseable
                continue
            if isinstance(doc, dict) and "blueprint" in doc:
                results.append({"path": p, "raw_url": _raw(p)})
                if len(results) >= cap:
                    break
    return {
        "ok": True,
        "repo": f"{owner}/{name}",
        "branch": branch,
        "detection": detection,
        "count": len(results),
        "blueprints": results,
        "truncated": bool((tree or {}).get("truncated")),
        "note": "Pass any raw_url to import_blueprint to deploy it.",
    }


def _device_signals(hass: HomeAssistant) -> dict[str, Any]:
    """Collect brand/domain signals describing what the user actually owns."""
    manufacturers: dict[str, int] = {}
    dev_reg = dr.async_get(hass)
    for dev in dev_reg.devices.values():
        man = (dev.manufacturer or "").strip()
        if man:
            manufacturers[man] = manufacturers.get(man, 0) + 1
    integration_domains = sorted(
        {e.domain for e in hass.config_entries.async_entries()}
    )
    entity_domains: dict[str, int] = {}
    for s in hass.states.async_all():
        d = s.entity_id.split(".", 1)[0]
        entity_domains[d] = entity_domains.get(d, 0) + 1
    return {
        "manufacturers": manufacturers,
        "integration_domains": integration_domains,
        "entity_domains": entity_domains,
    }


# Generically useful frontend cards worth recommending by entity-domain.
_CARD_HINTS = {
    "climate": ["thermostat", "climate"],
    "media_player": ["media", "mini-media-player"],
    "light": ["mushroom", "light"],
    "camera": ["webrtc", "frigate", "camera"],
    "vacuum": ["vacuum", "xiaomi-vacuum"],
    "weather": ["weather", "clock-weather"],
    "sensor": ["apexcharts", "mini-graph"],
    "person": ["person", "presence"],
}


async def recommend_resources(
    hass: HomeAssistant,
    limit: int = 15,
) -> dict[str, Any]:
    """Recommend HACS resources matched to the running HA's real devices.

    Reads device manufacturers, configured integrations and entity domains, then
    ranks HACS integrations and frontend cards that complement that hardware.
    The headline "match my devices to the right resources" capability.
    """
    signals = _device_signals(hass)
    brand_terms = [m.lower() for m in signals["manufacturers"]]
    # split multi-word manufacturers into tokens too (e.g. "Xiaomi Mi")
    for m in list(brand_terms):
        brand_terms.extend(w for w in m.split() if len(w) > 2)
    installed = set(signals["integration_domains"])

    try:
        integrations = await _hacs_catalog(hass, "integration")
        plugins = await _hacs_catalog(hass, "plugin")
    except Exception as err:  # noqa: BLE001
        return {"error": f"{type(err).__name__}: {err}"}

    integ_recs: list[dict[str, Any]] = []
    if brand_terms:
        ranked = [(it, _score(it, brand_terms)) for it in integrations]
        ranked = [(it, s) for it, s in ranked if s > 0]
        ranked.sort(key=lambda p: (p[1], p[0]["stars"]), reverse=True)
        seen: set[str] = set()
        for it, _ in ranked:
            dom = it.get("domain")
            if it["full_name"] in seen:
                continue
            seen.add(it["full_name"])
            reason = next(
                (b for b in signals["manufacturers"]
                 if b.lower() in (it["name"] + it["full_name"]
                                  + " ".join(it["topics"])).lower()),
                "matched your device brands",
            )
            integ_recs.append(
                {
                    **{k: it[k] for k in (
                        "name", "full_name", "description", "stars", "url",
                        "domain")},
                    "already_installed": dom in installed if dom else False,
                    "reason": f"You own {reason} devices",
                }
            )
            if len(integ_recs) >= limit:
                break

    # Frontend card recommendations driven by which entity domains exist.
    card_terms: list[str] = []
    for dom, count in signals["entity_domains"].items():
        if count and dom in _CARD_HINTS:
            card_terms.extend(_CARD_HINTS[dom])
    card_recs: list[dict[str, Any]] = []
    if card_terms:
        ranked_c = [(it, _score(it, card_terms)) for it in plugins]
        ranked_c = [(it, s) for it, s in ranked_c if s > 0]
        ranked_c.sort(key=lambda p: (p[1], p[0]["stars"]), reverse=True)
        for it, _ in ranked_c[:limit]:
            card_recs.append(
                {k: it[k] for k in (
                    "name", "full_name", "description", "stars", "url")}
            )

    return {
        "ok": True,
        "signals": {
            "manufacturers": signals["manufacturers"],
            "integration_domains": signals["integration_domains"],
            "entity_domain_counts": signals["entity_domains"],
        },
        "integration_recommendations": integ_recs,
        "card_recommendations": card_recs,
        "note": (
            "Install integrations/cards via HACS as a custom repository using "
            "the given GitHub url, or import a blueprint with import_blueprint."
        ),
    }


# Real entity domains -> the automation intents a user typically wants for them.
# Drives device-matched blueprint discovery (the "what can I automate with what
# I own" question) without the user knowing any search terms.
_DOMAIN_INTENTS = {
    "binary_sensor": [
        "motion activated light",
        "presence detection",
        "water leak alert",
        "door open notification",
    ],
    "light": ["motion activated light"],
    "sensor": ["low battery notification", "humidity alert", "temperature alert"],
    "lock": ["lock notification", "door left unlocked reminder"],
    "climate": ["thermostat schedule"],
    "cover": ["cover open reminder"],
    "person": ["presence detection", "leaving home reminder"],
    "device_tracker": ["presence detection"],
    "vacuum": ["vacuum notification"],
    "alarm_control_panel": ["alarm notification"],
}


async def recommend_blueprints(
    hass: HomeAssistant,
    limit: int = 12,
    preferred_intents: list[str] | None = None,
    imported_repos: set[str] | None = None,
) -> dict[str, Any]:
    """Recommend community blueprints matched to the home's real entity domains.

    Cross-source fusion of the device profile (which entity domains actually
    exist) with the community blueprint search: maps each present domain to the
    automation intents a user usually wants, searches for ready-made blueprints,
    and returns them deduped with the intent that surfaced each — so a non-expert
    gets "here are automations for what you own", then feeds a result to
    list_repo_blueprints -> import_blueprint. Bounded to a few searches.

    Memory-aware: ``preferred_intents`` (from the agent's memory) are searched
    first so the user's stated focus surfaces; repos in ``imported_repos`` (the
    import history) are flagged ``already_imported`` and demoted so fresh
    suggestions come first. Both are supplied by the dispatch layer, keeping
    this module decoupled from the memory store.
    """
    signals = _device_signals(hass)
    present = signals["entity_domains"]
    device_intents: list[str] = []
    seen_intent: set[str] = set()
    # Preferred intents (remembered) lead, then device-derived intents.
    for intent in (preferred_intents or []):
        if intent and intent not in seen_intent:
            seen_intent.add(intent)
            device_intents.append(intent)
    pref_count = len(device_intents)
    for dom in sorted(present, key=lambda d: present[d], reverse=True):
        for intent in _DOMAIN_INTENTS.get(dom, []):
            if intent not in seen_intent:
                seen_intent.add(intent)
                device_intents.append(intent)
    intents = device_intents[:4]  # cap external searches
    if not intents:
        return {
            "ok": True,
            "matched_domains": [],
            "recommendations": [],
            "note": "No entity domains map to known automation intents yet.",
        }

    imported = imported_repos or set()
    recs: list[dict[str, Any]] = []
    seen_repo: set[str] = set()
    errors: list[str] = []
    for intent in intents:
        res = await search_blueprints(hass, intent, limit=4)
        if res.get("error"):
            errors.append(f"{intent}: {res['error']}")
            continue
        for item in res.get("results", []):
            fn = item.get("full_name")
            if not fn or fn in seen_repo:
                continue
            seen_repo.add(fn)
            recs.append({
                **item,
                "matched_intent": intent,
                "already_imported": fn in imported,
            })
            if len(recs) >= limit:
                break
        if len(recs) >= limit:
            break
    # Fresh (not-yet-imported) first, then by stars.
    recs.sort(key=lambda r: (r.get("already_imported", False), -r.get("stars", 0)))
    out: dict[str, Any] = {
        "ok": True,
        "matched_domains": [d for d in present if d in _DOMAIN_INTENTS],
        "intents": intents,
        "preferred_intents_applied": intents[:pref_count],
        "count": len(recs),
        "recommendations": recs,
        "note": (
            "Call list_repo_blueprints on a full_name to get importable raw "
            "URLs, then import_blueprint."
        ),
    }
    if errors:
        out["partial_errors"] = errors
    return out


def _raw_url(url: str) -> str:
    """Best-effort convert a GitHub blob/gist URL to its raw form."""
    u = url.strip()
    if "github.com" in u and "/blob/" in u:
        u = u.replace("github.com", "raw.githubusercontent.com").replace(
            "/blob/", "/", 1
        )
    return u


async def import_blueprint(
    hass: HomeAssistant,
    store: dict,
    url: str,
    domain: str | None = None,
) -> dict[str, Any]:
    """Fetch a blueprint YAML by URL and import it into the HA config.

    Writes to ``blueprints/<domain>/ha_copilot/<slug>.yaml`` (the standard HA
    blueprint location), keeping a ``.copilot.bak`` of any prior file. The
    blueprint becomes selectable in the UI / usable via ``use_blueprint`` after
    the relevant domain reloads.
    """
    if not store.get(CONF_ALLOW_WRITE, True):
        return {"error": "writes are disabled (allow_write: false)"}
    try:
        text = await _fetch_text(hass, _raw_url(url))
    except Exception as err:  # noqa: BLE001
        return {"error": f"fetch failed: {type(err).__name__}: {err}"}
    try:
        doc = yaml.load(text, Loader=_BlueprintLoader)  # noqa: S506 - custom safe subclass
    except yaml.YAMLError as err:
        return {"error": f"invalid YAML: {err}"}
    if not isinstance(doc, dict) or "blueprint" not in doc:
        return {"error": "not a blueprint (missing top-level 'blueprint' key)"}
    meta = doc["blueprint"] or {}
    bp_domain = domain or meta.get("domain")
    if bp_domain not in ("automation", "script", "template"):
        return {
            "error": (
                "blueprint domain must be automation/script/template; got "
                f"{bp_domain!r} — pass domain= to override"
            )
        }
    name = meta.get("name") or url.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    slug = _slugify(name) or "imported"
    rel = os.path.join("blueprints", bp_domain, "ha_copilot", f"{slug}.yaml")
    target = _safe_path(hass, rel)
    os.makedirs(os.path.dirname(target), exist_ok=True)

    def _write() -> None:
        if os.path.exists(target):
            with open(target, encoding="utf-8") as f:
                prev = f.read()
            with open(target + ".copilot.bak", "w", encoding="utf-8") as f:
                f.write(prev)
        with open(target, "w", encoding="utf-8") as f:
            f.write(text)

    await hass.async_add_executor_job(_write)

    reloaded = False
    if hass.services.has_service(bp_domain, "reload"):
        try:
            await hass.services.async_call(bp_domain, "reload", blocking=True)
            reloaded = True
        except Exception:  # noqa: BLE001 - reload best-effort
            reloaded = False

    # HA references a blueprint by a path relative to ``blueprints/<domain>/``,
    # NOT including the domain segment. This is what validate_blueprint_inputs /
    # create_automation_from_blueprint / use_blueprint expect.
    blueprint_path = f"ha_copilot/{slug}.yaml"
    return {
        "ok": True,
        "name": name,
        "domain": bp_domain,
        "blueprint_path": blueprint_path,
        "file": rel,
        "reloaded": reloaded,
        "use": (
            f"Pass path='{blueprint_path}' (domain {bp_domain}) to "
            "validate_blueprint_inputs / create_automation_from_blueprint."
        ),
    }


def _url_quote(s: str) -> str:
    from urllib.parse import quote_plus

    return quote_plus(s)
