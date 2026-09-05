#!/usr/bin/env python3
"""
launcher_health.py - diagnostics for a Minecraft Bedrock server folder.

Every check returns a Result. Nothing here modifies the server; checks are
safe to run at any time, including while the server is up.

    from launcher_health import run_checks
    for r in run_checks(Path(".")):
        print(r.level, r.title, r.detail)

Run directly for a text report:
    python launcher_health.py [server-folder]
"""

from __future__ import annotations

import json
import argparse
import os
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from bedrock_storage import world_path

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
RANK = {FAIL: 0, WARN: 1, INFO: 2, OK: 3}


@dataclass
class Result:
    level: str
    title: str
    detail: str = ""
    fix: str = ""                     # what the user can do about it
    action: str = ""                  # key the launcher maps to a button
    data: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def read_props(root: Path) -> tuple[dict, list[str]]:
    """Returns {key: value} plus a list of duplicate keys."""
    props, dupes, seen = {}, [], set()
    p = root / "server.properties"
    if not p.exists():
        return props, dupes
    for line in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        s = line.strip()
        if s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        k, v = k.strip(), v.strip()
        if k in seen:
            dupes.append(k)
        seen.add(k)
        props[k] = v
    return props, dupes


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def pack_index(root: Path, folder: str) -> dict[str, dict]:
    """uuid -> {name, dir, files} for every pack folder on disk."""
    out: dict[str, dict] = {}
    base = root / folder
    if not base.is_dir():
        return out
    for d in base.iterdir():
        if not d.is_dir():
            continue
        m = read_json(d / "manifest.json", None)
        if not isinstance(m, dict):
            continue
        header = m.get("header") or {}
        uid = header.get("uuid")
        if not uid:
            continue
        name = str(header.get("name") or d.name)
        if name.startswith("pack."):
            name = d.name
        out[uid] = {"name": name, "dir": d}
    return out


def level_name(props: dict) -> str:
    return props.get("level-name", "Bedrock level")


# ---------------------------------------------------------------------------
# the checks
# ---------------------------------------------------------------------------

def check_layout(root: Path) -> list[Result]:
    out = []
    needed = {
        "bedrock_server.exe": "the server itself",
        "server.properties": "server configuration",
    }
    optional = {
        "server_manager.py": "scheduled restarts and player warnings",
        "player_history.py": "player history and the command queue",
        "bedrock_addons.py": "the addon installer",
        "build_mod_menu.py": "the in-game mod menu",
    }
    if sys.platform != "win32":
        needed.pop("bedrock_server.exe", None)
    for f, why in needed.items():
        if not (root / f).exists():
            out.append(Result(FAIL, f"{f} is missing", why,
                              "Use the Server Setup shortcut to select the correct server folder."))
    from app_paths import CODE_ROOT
    missing = [] if getattr(sys, 'frozen', False) else [f for f in optional if not (CODE_ROOT / f).exists()]
    if missing:
        out.append(Result(WARN, "Some helper scripts are missing",
                          ", ".join(missing),
                          "Those features are switched off until the files are restored."))
    if not out:
        out.append(Result(OK, "Server folder looks complete"))
    return out


def check_world(root: Path, props: dict) -> list[Result]:
    world = root / "worlds" / level_name(props)
    if not world.is_dir():
        return [Result(FAIL, "World folder not found", str(world),
                       "level-name in server.properties may not match the folder.")]
    db = world / "db"
    if not db.is_dir() or not any(db.iterdir()):
        return [Result(FAIL, "World database is empty", str(db),
                       "The world may be corrupt or was never generated.")]
    size = sum(f.stat().st_size for f in world.rglob("*") if f.is_file())
    return [Result(OK, "World is present",
                   f"{level_name(props)} - {size/1048576:.0f} MB",
                   data={"size": size})]


def check_packs(root: Path, props: dict) -> list[Result]:
    """The failure that broke this server: enabled in the world, missing on disk."""
    out: list[Result] = []
    world = root / "worlds" / level_name(props)
    for kind, wf, folder in (("behavior", "world_behavior_packs.json", "behavior_packs"),
                             ("resource", "world_resource_packs.json", "resource_packs")):
        entries = read_json(world / wf, [])
        if not isinstance(entries, list):
            out.append(Result(FAIL, f"{wf} is not valid JSON",
                              "The world cannot load its packs.",
                              "Delete the file to start with no packs, or restore a .bak."))
            continue
        have = pack_index(root, folder)
        for e in entries:
            if not isinstance(e, dict):
                continue
            uid = e.get("pack_id", "")
            if uid not in have:
                out.append(Result(
                    FAIL, f"A {kind} pack is enabled but not installed",
                    f"{uid} is listed in {wf} with no folder in {folder}/",
                    "The world will try to load a pack that isn't there. "
                    "Install it, or disable it on the Mods page.",
                    action="open_mods", data={"uuid": uid, "kind": kind}))
    if not out:
        out.append(Result(OK, "Every enabled pack is installed"))
    return out


def check_pack_integrity(root: Path) -> list[Result]:
    """A pack folder that looks half-copied."""
    out = []
    for folder in ("behavior_packs", "resource_packs"):
        base = root / folder
        if not base.is_dir():
            continue
        for d in base.iterdir():
            if not d.is_dir() or d.name.startswith(
                    ("vanilla", "chemistry", "editor", "experimental", "server_")):
                continue
            if not (d / "manifest.json").exists():
                out.append(Result(
                    WARN, "Pack folder has no manifest", str(d.name),
                    "Probably a half-finished copy. Reinstall it or delete the folder."))
                continue
            n = sum(1 for _ in d.rglob("*"))
            if n <= 2:
                out.append(Result(
                    WARN, "Pack folder looks nearly empty",
                    f"{d.name} contains {n} item(s)",
                    "If this pack misbehaves, reinstall it from mods/."))
    if not out:
        out.append(Result(OK, "Installed pack folders look intact"))
    return out


def check_security(root: Path, props: dict) -> list[Result]:
    out = []
    allow = props.get("allow-list", "false").lower() == "true"
    allowlist = read_json(root / "allowlist.json", [])
    online = props.get("online-mode", "true").lower() == "true"
    if not allow:
        out.append(Result(
            WARN, "Allowlist is off",
            "Anyone who knows your address can join.",
            "Turn allow-list on once your players are in allowlist.json.",
            action="open_settings"))
    elif not allowlist:
        out.append(Result(
            FAIL, "Allowlist is on but empty",
            "Nobody can join, including you.",
            "Add players with 'allowlist add <gamertag>' in the console, then restart."))
    else:
        out.append(Result(OK, "Allowlist is on",
                          f"{len(allowlist)} player(s) permitted"))
    if not online:
        out.append(Result(WARN, "online-mode is off",
                          "Players are not verified against Xbox Live.",
                          "Leave this on unless you have a specific reason."))
    if props.get("default-player-permission-level") == "operator":
        out.append(Result(WARN, "New players join as operators",
                          "Anyone who joins gets full command access.",
                          "Set default-player-permission-level to member.",
                          action="open_settings"))
    return out


def check_performance(root: Path, props: dict) -> list[Result]:
    out = []
    def as_int(key, default):
        try:
            return int(props.get(key, default))
        except ValueError:
            return None

    tick = as_int("tick-distance", 4)
    view = as_int("view-distance", 10)
    players = as_int("max-players", 10) or 10

    if tick is None:
        out.append(Result(WARN, "tick-distance is not a number", props.get("tick-distance", "")))
    elif not (4 <= tick <= 12):
        out.append(Result(
            FAIL, "tick-distance is out of range", f"currently {tick}, allowed 4-12",
            "This is the most CPU-expensive setting; cost rises with the square "
            "of the value. 6-8 suits a small server.", action="open_settings"))
    elif tick >= 10:
        out.append(Result(
            WARN, "tick-distance is high", f"{tick} chunks simulated around each player",
            "Fine on a fast machine. Drop to 8 if you see lag."))
    else:
        out.append(Result(OK, "tick-distance is sensible", f"{tick} chunks"))

    if view is not None and view > 24:
        out.append(Result(
            INFO, "view-distance is high", f"{view} chunks",
            f"Costs bandwidth and memory, roughly {view*view*players//1000}k "
            "chunk-slots at full capacity. Harmless if it performs well."))
    return out


def check_addon_link(root: Path, props: dict) -> list[Result]:
    """The in-game menu only works if the pack can talk through the console."""
    link_uuid = "7b3c1e42-9f5a-4d18-8c6b-2a4e7d905f11"
    installed = link_uuid in pack_index(root, "behavior_packs")
    if not installed:
        return [Result(INFO, "In-game menu pack is not installed",
                       "Restart Manager Link is absent",
                       "Install it from mods/ if you want /scriptevent mgr:menu.")]
    if props.get("content-log-console-output-enabled", "false").lower() != "true":
        return [Result(
            FAIL, "In-game menu cannot reach the launcher",
            "content-log-console-output-enabled is false",
            "The pack talks to the launcher through the console log. "
            "Turn this on and restart.", action="open_settings")]
    return [Result(OK, "In-game menu link is configured")]


def check_history_db(root: Path) -> list[Result]:
    p = root / "players.db"
    if not p.exists():
        return [Result(INFO, "No player history yet",
                       "It is created the first time the launcher runs.")]
    db = None
    try:
        db = sqlite3.connect(p.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
        integrity = db.execute('PRAGMA quick_check').fetchone()[0]
        if integrity != 'ok':
            raise sqlite3.DatabaseError(integrity)
        n = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        players = db.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        db.close()
        return [Result(OK, "Player history is healthy",
                       f"{players} player(s), {n} event(s) recorded")]
    except sqlite3.DatabaseError as exc:
        return [Result(
            FAIL, "Player history database is damaged", str(exc),
            "Close and reopen the launcher - it moves the bad file aside "
            "and starts a clean one.")]
    except Exception as exc:
        return [Result(WARN, "Could not read the history database", str(exc))]
    finally:
        if db is not None:
            db.close()


def check_backups(root: Path, props: dict) -> list[Result]:
    folder = root / "backups"
    zips = sorted(folder.glob("*.zip"), key=lambda f: f.stat().st_mtime,
                  reverse=True) if folder.is_dir() else []
    if not zips:
        return [Result(WARN, "No world backups", "backups/ is empty or missing",
                       "One bad pack can cost you the world. Make a backup.",
                       action="open_backups")]
    newest = datetime.fromtimestamp(zips[0].stat().st_mtime)
    age = datetime.now() - newest
    total = sum(f.stat().st_size for f in zips)
    if age > timedelta(days=7):
        return [Result(WARN, "Newest backup is old",
                       f"{zips[0].name} - {age.days} days ago",
                       "Make a fresh one before changing packs.",
                       action="open_backups")]
    return [Result(OK, f"{len(zips)} backup(s)",
                   f"newest {age.days}d {age.seconds//3600}h old, "
                   f"{total/1048576:.0f} MB total")]


def check_disk(root: Path) -> list[Result]:
    try:
        usage = shutil.disk_usage(root)
    except OSError as exc:
        return [Result(WARN, "Could not read disk space", str(exc))]
    free_gb = usage.free / (1024 ** 3)
    pct = usage.free / usage.total * 100
    if free_gb < 2:
        lvl, fix = FAIL, "The server can corrupt a world if it cannot write."
    elif free_gb < 10:
        lvl, fix = WARN, "Backups and world growth need room."
    else:
        lvl, fix = OK, ""
    return [Result(lvl, f"{free_gb:.0f} GB disk free",
                   f"{pct:.0f}% of the drive", fix,
                   data={"free": usage.free, "total": usage.total})]


def check_props_syntax(root: Path) -> list[Result]:
    props, dupes = read_props(root)
    if not props:
        return [Result(FAIL, "server.properties is empty or unreadable")]
    if dupes:
        return [Result(WARN, "Duplicate settings in server.properties",
                       ", ".join(sorted(set(dupes))),
                       "The last one wins. Remove the earlier copies.")]
    return [Result(OK, "server.properties parses cleanly",
                   f"{len(props)} settings")]


def check_python(root: Path) -> list[Result]:
    v = sys.version_info
    if v < (3, 10):
        return [Result(FAIL, "Python is too old",
                       f"{v.major}.{v.minor} - 3.10 or newer is needed")]
    return [Result(OK, "Python is fine", f"{v.major}.{v.minor}.{v.micro}")]


# ---------------------------------------------------------------------------

def validate_properties(props: dict) -> list[str]:
    errors = []
    for key, low, high in (('server-port', 1, 65535), ('server-portv6', 1, 65535),
                           ('max-players', 1, 100000), ('tick-distance', 4, 12),
                           ('view-distance', 5, 100000), ('player-idle-timeout', 0, 100000)):
        if key not in props:
            continue
        try:
            value = int(props[key])
            if not low <= value <= high:
                raise ValueError()
        except (TypeError, ValueError):
            errors.append(f'{key}: enter a whole number between {low} and {high}.')
    for key, allowed in {'gamemode': ('survival', 'creative', 'adventure'),
                         'difficulty': ('peaceful', 'easy', 'normal', 'hard'),
                         'default-player-permission-level': ('visitor', 'member', 'operator')}.items():
        if key in props and props[key] not in allowed:
            errors.append(f'{key}: choose {", ".join(allowed)}.')
    for key in ('online-mode', 'allow-list', 'allow-cheats', 'content-log-console-output-enabled'):
        if key in props and props[key] not in ('true', 'false'):
            errors.append(f'{key}: enter true or false.')
    for key, value in props.items():
        if '\n' in str(value) or '\r' in str(value):
            errors.append(f'{key}: use one line.')
    return errors


def check_configuration(root: Path, props: dict) -> list[Result]:
    errors = validate_properties(props)
    try:
        world_path(root, props.get('level-name', 'Bedrock level'))
    except ValueError as exc:
        errors.append(str(exc))
    for name in ('manager_config.json', 'launcher_ui.json'):
        path = root / name
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
            if not isinstance(data, dict):
                raise ValueError('Expected a JSON object')
            if name == 'manager_config.json':
                import server_manager
                try:
                    server_manager.parse_times(data.get('restart_times', server_manager.RESTART_TIMES))
                except (SystemExit, TypeError) as exc:
                    raise ValueError(str(exc)) from exc
        except (OSError, ValueError) as exc:
            errors.append(f'{name}: {exc}')
    return [Result(FAIL, 'Configuration needs attention', '\n'.join(errors),
                   'Fix these values before starting the server.', action='open_settings')] if errors else [
                       Result(OK, 'Configuration values are valid')]


def check_recovery(root: Path) -> list[Result]:
    from bedrock_recovery import operations
    pending = operations(root, True)
    if pending:
        return [Result(FAIL, 'Interrupted changes need recovery',
            '; '.join(f"{item.get('kind')}: {item.get('state')}" for item in pending),
            'Restore the journalled files before starting or changing the server.', 'open_recovery')]
    return [Result(OK, 'No interrupted operations')]


def check_dependencies(root: Path) -> list[Result]:
    from bedrock_mods import pack_index, active_packs, dependency_issues
    index, duplicates = pack_index(root)
    enabled = {str(e['pack_id']).lower() for entries in active_packs(root).values() for e in entries}
    issues = dependency_issues(index, enabled) + [f'Duplicate pack UUID: {u}' for u in duplicates]
    if issues:
        return [Result(WARN, 'Pack dependencies need attention', '\n'.join(issues),
            'Compare pack versions and requirements before the next startup.', 'open_dependencies')]
    return [Result(OK, 'Enabled pack dependencies match')]


def run_checks(root: Path) -> list[Result]:
    root = Path(root)
    props, _ = read_props(root)
    results: list[Result] = []
    for fn, args in (
        (check_layout, (root,)),
        (check_props_syntax, (root,)),
        (check_configuration, (root, props)),
        (check_world, (root, props)),
        (check_packs, (root, props)),
        (check_pack_integrity, (root,)),
        (check_addon_link, (root, props)),
        (check_security, (root, props)),
        (check_performance, (root, props)),
        (check_history_db, (root,)),
        (check_backups, (root, props)),
        (check_disk, (root,)),
        (check_python, (root,)),
        (check_recovery, (root,)),
        (check_dependencies, (root,)),
    ):
        try:
            results.extend(fn(*args))
        except Exception as exc:
            results.append(Result(WARN, f"Check '{fn.__name__}' failed", str(exc)))
    results.sort(key=lambda r: RANK.get(r.level, 9))
    return results


def summary(results: list[Result]) -> dict:
    out = {OK: 0, WARN: 0, FAIL: 0, INFO: 0}
    for r in results:
        out[r.level] = out.get(r.level, 0) + 1
    return out


def as_text(root: Path) -> str:
    """A plain-text report - handy to paste when asking for help."""
    results = run_checks(root)
    s = summary(results)
    lines = [f"Bedrock server diagnostics - {datetime.now():%Y-%m-%d %H:%M}",
             f"Folder: {root}",
             f"{s[FAIL]} problem(s), {s[WARN]} warning(s), {s[OK]} ok", ""]
    mark = {OK: "[ ok ]", WARN: "[warn]", FAIL: "[FAIL]", INFO: "[info]"}
    for r in results:
        lines.append(f"{mark.get(r.level,'[    ]')} {r.title}")
        if r.detail:
            lines.append(f"         {r.detail}")
        if r.fix:
            lines.append(f"      -> {r.fix}")
    props, _ = read_props(root)
    lines += ["", "Key settings:"]
    for k in ("level-name", "max-players", "allow-list", "online-mode",
              "view-distance", "tick-distance", "player-idle-timeout",
              "content-log-console-output-enabled"):
        lines.append(f"  {k}={props.get(k, '(unset)')}")
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Read-only Bedrock diagnostics.')
    parser.add_argument('server', type=Path, nargs='?', default=Path(__file__).resolve().parent)
    parser.add_argument('--json', action='store_true', help='Machine-readable diagnostic results')
    args = parser.parse_args()
    results = run_checks(args.server)
    if args.json:
        print(json.dumps({'summary': summary(results), 'checks': [asdict(r) for r in results]}, indent=2))
    else:
        print(as_text(args.server))
    sys.exit(1 if any(r.level == FAIL for r in results) else 0)
