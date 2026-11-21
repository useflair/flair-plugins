#!/usr/bin/env python3
"""Sync installable per-agent plugins in this marketplace from the flair catalog.

Reads the executor's public catalog (`/catalog.json`) and, for every *published*
expert agent, (re)generates a thin per-agent wrapper plugin in both Claude
(`<slug>-claude/`) and Codex (`<slug>-codex/`) form and upserts both marketplace
indexes. Stdlib only — no `useflair` dependency — so it runs in plain CI.

Safety:
- Only touches wrappers it generated (marked with a `.flair-generated` file).
  Hand-maintained plugins (`flair-builder`, a curated
  `recruiting-copilot`, …) have no marker and are left completely alone.
- Prunes generated wrappers whose agent has left the catalog.
- Writes only on real content change, and output is deterministic — an
  unchanged catalog produces no git diff (so no commit churn).

Usage:
  python3 scripts/sync_marketplace.py --catalog-url <URL-or-path> --root .
"""
from __future__ import annotations

import argparse
import json
import shutil
import urllib.request
from pathlib import Path

REPO = "https://github.com/useflair/flair-plugins"
MARKER = ".flair-generated"
AUTHOR = {"name": "flair", "url": "https://useflair.io"}
# Per-agent storefront host — used for each wrapper's `homepage`/`websiteURL`.
# Must be per-agent, not the generic site-wide homepage: Claude Desktop's
# connector icon is resolved from this field's favicon, not from `icon`/
# `iconURL` (which Claude's plugin schema doesn't recognize at all) — a
# shared generic `homepage` made every installed agent show the same site
# favicon instead of its own logo.
MARKETPLACE_HOST = "https://marketplace.useflair.io"
CLAUDE_SUFFIX = "-claude"
CODEX_SUFFIX = "-codex"


def load_catalog(src: str) -> list:
    """Load the catalog from an http(s) URL or a local path."""
    if src.startswith(("http://", "https://")):
        with urllib.request.urlopen(src, timeout=30) as r:  # noqa: S310
            data = json.loads(r.read().decode())
    else:
        data = json.loads(Path(src).read_text(encoding="utf-8"))
    agents = data.get("agents", data) if isinstance(data, dict) else data
    return [a for a in agents if isinstance(a, dict) and a.get("slug")]


def _write_if_changed(path: Path, content: str) -> bool:
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def _write_bytes_if_changed(path: Path, content: bytes) -> bool:
    if path.is_file() and path.read_bytes() == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return True


def _write_json(path: Path, obj) -> bool:
    return _write_if_changed(path, json.dumps(obj, indent=2) + "\n")


_ICON_EXT_BY_CONTENT_TYPE = {
    "image/svg+xml": ".svg", "image/png": ".png",
    "image/jpeg": ".jpg", "image/webp": ".webp",
}


def _fetch_icon(url: str) -> tuple[bytes, str] | None:
    """Download an agent's icon so Codex can bundle it as a local asset —
    its plugin schema has no field for a *remote* icon URL, only a
    relative-path `logo`/`composerIcon` (see the hand-maintained
    `flair-builder` plugin)."""
    try:
        with urllib.request.urlopen(url, timeout=30) as r:  # noqa: S310
            ct = r.headers.get_content_type()
            return r.read(), _ICON_EXT_BY_CONTENT_TYPE.get(ct, ".png")
    except OSError:
        return None


def _service_price(s: dict) -> str:
    """A service's price as the wrapper states it (the same words as the
    engine's `publish._service_row`). Empty for an older catalog that carries
    only id + name."""
    if "price_credits" not in s and "billing" not in s:
        return ""
    price, billing = s.get("price_credits"), s.get("billing") or "usage"
    if price is None or billing == "usage":
        return "usage-metered"
    if billing == "success_fee":
        return f"{price} cr — only if the rubric is satisfied"
    if billing == "per_minute":
        return f"{price} cr per minute — a live voice call"
    return f"{price} cr"


def _service_line(s: dict) -> str:
    line = f"- **{s.get('name') or s['id']}** (tool `{s['id']}`)"
    if s.get("description"):
        line += f" — {s['description']}"
    facts = [_service_price(s)]
    if s.get("expected_duration_s"):
        facts.append(f"about {s['expected_duration_s']}s")
    if s.get("intake_questions"):
        facts.append(f"asks {s['intake_questions']} question(s) before it starts")
    facts = [f for f in facts if f]
    return f"{line}\n  {'; '.join(facts)}." if facts else line


# The engine's `frontdesk.WRAPPER_NOTE`, kept word for word: the plugin's own
# page answers questions about the agent, and nothing answers how it works.
WRAPPER_NOTE = (
    "- **Questions about this agent** (what it does, what a service costs, "
    "takes or needs): answer from this page. The `ask` tool answers from the "
    "same public facts. The agent never explains how it works behind the "
    "scenes, so do not ask it to.")


def _skill_md(a: dict, endpoint: str) -> str:
    skills = "\n".join(
        _service_line(s) for s in a.get("skills", [])
    ) or "- (this agent exposes a single `ask` tool)"
    return f"""---
name: {a['slug']}
description: {a['description']}
---

# {a['display_name']}

{a['description']}

This plugin is a thin wrapper around the **{a['display_name']}** expert agent,
which runs **hosted** on the flair marketplace. The reasoning happens
server-side; this plugin connects your app to it over MCP (`{endpoint}`). Using
it signs you in to flair.

## What it can do

{skills}

Call the tool that matches what you need; the hosted agent does the work and
returns the result. Each tool's description states its price, typical
duration, and the intake it asks for.

## Conduct (for the model)

The platform surfaces are self-describing — follow them exactly:
- **Every tool result tells you the next step.** A paused run returns its
  questions and says to relay them to your user; a pending run says how long
  to sleep and carries live `status` to share; the final result is the
  deliverable. Do what the payload says — nothing more.
- A hosted run can take minutes; that latency is normal, not a failure.
- Never fabricate progress, results, or setup errors around a call — the
  result IS the returned text.
{WRAPPER_NOTE}
"""


def is_generated(d: Path) -> bool:
    return (d / MARKER).is_file()


def write_wrapper(root: Path, a: dict) -> None:
    slug = a["slug"]
    ver = a.get("version") or "0.1.0"
    endpoint = a.get("endpoint") or f"https://agents.useflair.io/mcp/{slug}/"
    tags = a.get("tags") or []
    category = a.get("category") or "Other"
    mcp = {"mcpServers": {slug: {"type": "http", "url": endpoint}}}
    skill = _skill_md(a, endpoint)
    note = "generated by scripts/sync_marketplace.py — do not hand-edit\n"
    icon = a.get("icon")
    homepage = f"{MARKETPLACE_HOST}/agents/{slug}"

    cl = root / f"{slug}{CLAUDE_SUFFIX}"
    _write_json(cl / ".claude-plugin" / "plugin.json", {
        "name": slug, "displayName": a["display_name"], "version": ver,
        "description": a["description"], "author": AUTHOR,
        "homepage": homepage, "repository": REPO, "license": "MIT",
        "keywords": tags, "icon": icon,
    })
    _write_json(cl / ".mcp.json", mcp)
    _write_if_changed(cl / "skills" / slug / "SKILL.md", skill)
    _write_if_changed(cl / MARKER, note)

    cx = root / f"{slug}{CODEX_SUFFIX}"
    interface = {
        "displayName": a["display_name"], "shortDescription": a["description"],
        "longDescription": a["description"] + " Runs hosted; install to use it.",
        "developerName": "flair", "category": category,
        "websiteURL": homepage,
    }
    fetched = _fetch_icon(icon) if icon else None
    if fetched:
        data, ext = fetched
        _write_bytes_if_changed(cx / "assets" / f"logo{ext}", data)
        interface["logo"] = interface["composerIcon"] = f"./assets/logo{ext}"
    _write_json(cx / ".codex-plugin" / "plugin.json", {
        "name": slug, "version": ver, "description": a["description"],
        "author": AUTHOR, "homepage": homepage, "repository": REPO,
        "license": "MIT", "keywords": tags, "skills": "./skills/",
        "mcpServers": "./.mcp.json",
        "interface": interface,
    })
    _write_json(cx / ".mcp.json", mcp)
    _write_if_changed(cx / "skills" / slug / "SKILL.md", skill)
    _write_if_changed(cx / MARKER, note)


def upsert_index(path: Path, name: str, entry: dict) -> None:
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"plugins": []}
    plugins = data.setdefault("plugins", [])
    plugins[:] = [p for p in plugins if p.get("name") != name] + [entry]
    _write_json(path, data)


def remove_from_index(path: Path, name: str) -> None:
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    data["plugins"] = [p for p in data.get("plugins", []) if p.get("name") != name]
    _write_json(path, data)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--catalog-url", required=True, help="URL or path to catalog.json")
    ap.add_argument("--root", default=".", help="plugins repo root")
    args = ap.parse_args()

    root = Path(args.root)
    claude_idx = root / ".claude-plugin" / "marketplace.json"
    codex_idx = root / ".agents" / "plugins" / "marketplace.json"

    agents = load_catalog(args.catalog_url)
    catalog_slugs = {a["slug"] for a in agents}

    added, updated, skipped, pruned = [], [], [], []
    for agent in agents:
        slug = agent["slug"]
        cl = root / f"{slug}{CLAUDE_SUFFIX}"
        if cl.exists() and not is_generated(cl):
            skipped.append(slug)  # hand-maintained — never touch
            continue
        existed = cl.exists()
        write_wrapper(root, agent)
        upsert_index(claude_idx, slug, {
            "name": slug, "source": f"./{slug}{CLAUDE_SUFFIX}",
            "description": agent["description"], "version": agent.get("version") or "0.1.0",
            "homepage": f"{MARKETPLACE_HOST}/agents/{slug}",
            "repository": REPO, "license": "MIT",
            "category": agent.get("category") or "Other", "tags": agent.get("tags") or [],
        })
        upsert_index(codex_idx, slug, {
            "name": slug, "source": {"source": "local", "path": f"./{slug}{CODEX_SUFFIX}"},
            "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
            "category": agent.get("category") or "Other", "description": agent["description"],
        })
        (updated if existed else added).append(slug)

    # Prune generated wrappers whose agent has left the catalog (never prune a
    # hand-maintained dir — it has no marker).
    for cl in sorted(root.glob(f"*{CLAUDE_SUFFIX}")):
        if not is_generated(cl):
            continue
        slug = cl.name[: -len(CLAUDE_SUFFIX)]
        if slug in catalog_slugs:
            continue
        shutil.rmtree(cl, ignore_errors=True)
        shutil.rmtree(root / f"{slug}{CODEX_SUFFIX}", ignore_errors=True)
        remove_from_index(claude_idx, slug)
        remove_from_index(codex_idx, slug)
        pruned.append(slug)

    print(f"added={added} updated={updated} "
          f"skipped(hand-maintained)={skipped} pruned={pruned}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
