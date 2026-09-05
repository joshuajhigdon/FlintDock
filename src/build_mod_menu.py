#!/usr/bin/env python3
"""
build_mod_menu.py - inventory the behavior packs installed on this server and
generate the in-game Mod Control Center menu.

Bedrock has no standard "settings" format, so this looks for the thing packs
actually use: .mcfunction files. Anything a pack exposes as a function becomes
a runnable button in the in-game menu.

    python build_mod_menu.py            # scan, print a report, write catalog.js
    python build_mod_menu.py --report   # scan and print only, write nothing
    python build_mod_menu.py --all      # include every function, not just
                                        # ones that look like settings

Run it after installing or updating packs, then restart the server.
Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from bedrock_storage import atomic_text, operation_lock

# The Restart Manager Link pack - where the generated catalog is written.
MENU_PACK_UUID = "7b3c1e42-9f5a-4d18-8c6b-2a4e7d905f11"

# Folder names that usually mean "this is a setting, not internal machinery".
SETTING_HINTS = ("config", "setting", "settings", "toggle", "toggles",
                 "option", "options", "menu", "setup", "feature", "features")

# Function names that are almost certainly internal plumbing.
NOISE = re.compile(
    r"(^|/)(tick|loop|init|internal|lib|util|core|debug|_)", re.IGNORECASE)

MAX_FUNCS_PER_PACK = 60
MAX_LABEL = 48


def resolve_name(pack_dir: Path, raw: str, fallback: str) -> str:
    """manifest names are often 'pack.name' - look up the real string."""
    if not raw.startswith("pack."):
        return raw or fallback
    for lang in ("en_US.lang", "en_GB.lang"):
        f = pack_dir / "texts" / lang
        if not f.exists():
            continue
        try:
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                key, _, value = line.partition("=")
                if key.strip() == raw:
                    return value.split("##")[0].strip() or fallback
        except OSError:
            pass
    return fallback


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None


def find_server_root(start: Path) -> Path:
    if (start / "server.properties").exists():
        return start
    raise SystemExit(
        f"No server.properties in {start}\n"
        "Put this next to bedrock_server.exe, or pass --server <path>.")


def level_name(root: Path) -> str:
    for line in (root / "server.properties").read_text(
            encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == "level-name":
            return v.strip()
    return "Bedrock level"


def active_pack_ids(root: Path, world: str) -> set[str]:
    """UUIDs listed in world_behavior_packs.json - the packs actually on."""
    data = read_json(root / "worlds" / world / "world_behavior_packs.json")
    if not isinstance(data, list):
        return set()
    return {e.get("pack_id") for e in data
            if isinstance(e, dict) and e.get("pack_id")}


def describe_function(path: Path) -> str:
    """Use the function's own leading # comments as its description."""
    try:
        from itertools import islice
        with path.open(encoding='utf-8', errors='replace') as stream:
            lines = list(islice(stream, 8))
    except OSError:
        return ""
    out = []
    for line in lines[:8]:
        line = line.strip()
        if not line:
            if out:
                break
            continue
        if not line.startswith("#"):
            break
        text = line.lstrip("#").strip()
        if text:
            out.append(text)
    return " ".join(out)[:120]


def looks_like_setting(rel: str) -> bool:
    """
    Only functions living in a settings-ish FOLDER count.

    Most packs keep internal machinery as loose top-level functions
    (summon_x, generate_y). Surfacing those as "settings" buttons is
    actively dangerous, so a function has to be deliberately filed under
    config/ or settings/ to earn a place in the menu. --all overrides this.
    """
    parts = rel.lower().split("/")
    if len(parts) < 2:
        return False
    if NOISE.search(rel.lower()):
        return False
    return any(hint in part for part in parts[:-1] for hint in SETTING_HINTS)


# .toggle("Label", { defaultValue: player.hasTag("tag") })  - the common way
# script-driven packs store per-player settings.
# The delimiter is captured and back-referenced so a label may contain the
# OTHER quote character - e.g. "Enables the 'WAILA' UI".
RE_TAG_TOGGLE = re.compile(
    r"""\.toggle\(\s*(["'])((?:(?!\1).){2,90})\1\s*,\s*\{\s*"""
    r"""defaultValue\s*:\s*player\.hasTag\(\s*(["'])((?:(?!\3).)+)\3\s*\)""",
    re.VERBOSE)

# new ItemStack("namespace:something_config") - a pack's own settings item
RE_CONFIG_ITEM = re.compile(
    r"""ItemStack\(\s*["']([a-z0-9_]+:[a-z0-9_]*config[a-z0-9_]*)["']""")


def scan_scripts(pack_dir: Path) -> tuple[list[dict], list[str]]:
    """Find per-player tag toggles and any config item the pack hands out."""
    toggles: list[dict] = []
    items: list[str] = []
    seen_tags: set[str] = set()
    sdir = pack_dir / "scripts"
    if not sdir.is_dir():
        return toggles, items
    for f in sorted(sdir.rglob("*.js"))[:400]:
        try:
            if f.stat().st_size > 4 * 1024**2:
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for _q1, label, _q2, tag in RE_TAG_TOGGLE.findall(text):
            if tag in seen_tags:
                continue
            seen_tags.add(tag)
            name, _, detail = label.partition(" - ")
            toggles.append({
                "label": name.strip()[:MAX_LABEL],
                "tag": tag,
                "desc": detail.strip()[:120],
            })
        for item in RE_CONFIG_ITEM.findall(text):
            if item not in items:
                items.append(item)
    return toggles, items


def scan_pack(pack_dir: Path, include_all: bool) -> dict | None:
    manifest = read_json(pack_dir / "manifest.json")
    if not isinstance(manifest, dict):
        return None
    header = manifest.get("header") or {}
    if not isinstance(header, dict):
        return None
    uuid = header.get("uuid")
    if not uuid:
        return None

    funcs = []
    fdir = pack_dir / "functions"
    if fdir.is_dir():
        for f in sorted(fdir.rglob("*.mcfunction")):
            rel = f.relative_to(fdir).with_suffix("").as_posix()
            if not include_all and not looks_like_setting(rel):
                continue
            funcs.append({
                "cmd": rel,
                "label": rel.split("/")[-1].replace("_", " ")[:MAX_LABEL],
                "path": rel,
                "desc": describe_function(f),
            })

    truncated = False
    if len(funcs) > MAX_FUNCS_PER_PACK:
        funcs = funcs[:MAX_FUNCS_PER_PACK]
        truncated = True

    toggles, config_items = scan_scripts(pack_dir)

    subpacks = []
    for sp in manifest.get("subpacks", []) or []:
        if isinstance(sp, dict) and sp.get("name"):
            subpacks.append({"name": str(sp["name"]),
                             "tier": sp.get("memory_tier")})

    version = header.get("version")
    if isinstance(version, list):
        version = ".".join(str(v) for v in version)

    return {
        "uuid": uuid,
        "name": resolve_name(pack_dir, str(header.get("name") or ""), pack_dir.name),
        "toggles": toggles,
        "config_items": config_items,
        "desc": resolve_name(pack_dir, str(header.get("description") or ""), "")[:200],
        "version": str(version or "?"),
        "folder": pack_dir.name,
        "functions": funcs,
        "subpacks": subpacks,
        "truncated": truncated,
    }


def find_menu_pack(root: Path) -> Path | None:
    bp = root / "behavior_packs"
    if not bp.is_dir():
        return None
    for d in bp.iterdir():
        if not d.is_dir():
            continue
        m = read_json(d / "manifest.json")
        if isinstance(m, dict) and isinstance(m.get('header'), dict) and m['header'].get('uuid') == MENU_PACK_UUID:
            return d
    return None


def write_catalog(target: Path, packs: list[dict]) -> None:
    payload = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "packs": [
            {
                "name": p["name"],
                "version": p["version"],
                "desc": p["desc"],
                "uuid": p["uuid"],
                "truncated": p["truncated"],
                "subpacks": [s["name"] for s in p["subpacks"]],
                "toggles": p["toggles"],
                "configItems": p["config_items"],
                "functions": [
                    {"label": f["label"], "cmd": f["cmd"], "desc": f["desc"]}
                    for f in p["functions"]
                ],
            }
            for p in packs
        ],
    }
    try:
        previous = target.read_text(encoding='utf-8-sig').split('export const CATALOG = ', 1)[1].strip().removesuffix(';')
        if json.loads(previous).get('packs') == payload['packs']:
            return  # No timestamp-only package changes or unnecessary restore points.
    except (OSError, ValueError, IndexError, AttributeError):
        pass
    body = json.dumps(payload, indent=1, ensure_ascii=False)
    atomic_text(target,
        "// GENERATED by build_mod_menu.py - do not edit by hand.\n"
        "// Re-run that script after adding or updating packs.\n"
        f"export const CATALOG = {body};\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the in-game Mod Control Center menu.")
    ap.add_argument("--server", type=Path, default=Path(__file__).resolve().parent)
    ap.add_argument("--report", action="store_true", help="Print only, write nothing")
    ap.add_argument("--all", action="store_true",
                    help="Include every function, not just likely settings")
    args = ap.parse_args()

    root = find_server_root(args.server.resolve())
    world = level_name(root)
    active = active_pack_ids(root, world)

    bp_dir = root / "behavior_packs"
    if not bp_dir.is_dir():
        print(f"No behavior_packs folder in {root}")
        return 1

    packs = []
    for d in sorted(bp_dir.iterdir()):
        if not d.is_dir():
            continue
        info = scan_pack(d, args.all)
        if not info:
            continue
        info["active"] = info["uuid"] in active
        packs.append(info)

    print(f"Server : {root}")
    print(f"World  : {world}")
    print(f"Found  : {len(packs)} behavior pack(s), "
          f"{sum(1 for p in packs if p['active'])} active\n")

    for p in packs:
        mark = "ON " if p["active"] else "off"
        print(f"[{mark}] {p['name']}  v{p['version']}")
        print(f"       folder: {p['folder']}")
        if p["desc"]:
            print(f"       {p['desc'][:100]}")
        if p["subpacks"]:
            print(f"       subpacks: {', '.join(s['name'] for s in p['subpacks'])}")
        if p["toggles"]:
            print(f"       {len(p['toggles'])} per-player toggle(s) via player tags:")
            for t in p["toggles"]:
                extra = f"  - {t['desc'][:50]}" if t["desc"] else ""
                print(f"         [{t['tag']}] {t['label']}{extra}")
        if p["config_items"]:
            print(f"       config item(s): {', '.join(p['config_items'])}")
        if p["functions"]:
            print(f"       {len(p['functions'])} function(s)"
                  + ("  [truncated]" if p["truncated"] else ""))
            for f in p["functions"][:12]:
                extra = f"  - {f['desc'][:60]}" if f["desc"] else ""
                print(f"         /function {f['cmd']}{extra}")
            if len(p["functions"]) > 12:
                print(f"         ... and {len(p['functions']) - 12} more")
        elif not p["toggles"] and not p["config_items"]:
            print("       no settings detected "
                  "(no config functions, tag toggles or config item)")
        print()

    menu_packs = [p for p in packs if p["active"] and p["uuid"] != MENU_PACK_UUID
                  and (p["functions"] or p["toggles"] or p["config_items"])]

    if args.report:
        print("--report given: catalog.js not written.")
        return 0

    target_dir = find_menu_pack(root)
    if target_dir is None:
        print("Restart Manager Link pack not found in behavior_packs/.")
        print("Install RestartManagerLink.mcaddon first, then re-run this.")
        return 1

    scripts = target_dir / "scripts"
    try:
        with operation_lock(root):
            from bedrock_update import server_running
            from build_admin_addon import install
            import tempfile
            if server_running():
                raise RuntimeError('Stop the server before rebuilding the in-game menu.')
            # Re-read selection under the lock. Preserve an explicit disabled profile.
            active = active_pack_ids(root, world)
            if MENU_PACK_UUID not in active:
                print('In-game tools are disabled. Use the launcher Install / update action to enable them.')
                return 0
            menu_packs = []
            for folder in sorted(bp_dir.iterdir()):
                if not folder.is_dir() or folder.is_symlink():
                    continue
                header = (read_json(folder / 'manifest.json') or {}).get('header', {})
                if header.get('uuid') not in active - {MENU_PACK_UUID}:
                    continue
                info = scan_pack(folder, args.all)
                if info and (info['functions'] or info['toggles'] or info['config_items']):
                    menu_packs.append(info)
            with tempfile.TemporaryDirectory(prefix='.menu-build-', dir=root) as temp:
                catalog = Path(temp) / 'catalog.js'
                if (scripts / 'catalog.js').is_file():
                    atomic_text(catalog, (scripts / 'catalog.js').read_text(encoding='utf-8-sig'))
                write_catalog(catalog, menu_packs)
                install(root, lock_held=True, catalog_override=catalog)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f'Could not write the menu: {exc}')
        return 1
    n_fn = sum(len(p["functions"]) for p in menu_packs)
    n_tg = sum(len(p["toggles"]) for p in menu_packs)
    n_it = sum(len(p["config_items"]) for p in menu_packs)
    print('Verified mod menu and command help; package, world registration and installer state synchronized.')
    print(f"  {len(menu_packs)} pack(s): {n_tg} toggle(s), {n_fn} function(s), "
          f"{n_it} config item(s).")
    print("\nRestart the server, then run /scriptevent mgr:menu in game.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
