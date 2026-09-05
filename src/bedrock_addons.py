#!/usr/bin/env python3
"""
bedrock_addons.py - install / uninstall Minecraft Bedrock addons on a
dedicated server from a "mods" drop folder.

Put this file in your bedrock-server directory (next to bedrock_server.exe),
create a folder called "mods" beside it, and drop .mcaddon / .mcpack files in.

    python bedrock_addons.py install      # install anything new in mods/
    python bedrock_addons.py list         # show what's installed
    python bedrock_addons.py uninstall NAME
    python bedrock_addons.py uninstall --all
    python bedrock_addons.py verify       # re-check installed packs are intact

Archives are left in mods/ permanently. State is tracked in
mods/_addon_state.json, keyed by file content hash, so re-running install
does nothing unless a file is new or has changed.

Stop the server before running this. Stdlib only, no pip installs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
import uuid as uuidlib
from datetime import datetime
from pathlib import Path
from bedrock_storage import atomic_json, extract_zip, world_path, operation_lock

ARCHIVE_SUFFIXES = {".mcaddon", ".mcpack", ".zip"}
STATE_FILENAME = "_addon_state.json"

BEHAVIOR = "behavior"
RESOURCE = "resource"

WORLD_JSON = {
    BEHAVIOR: "world_behavior_packs.json",
    RESOURCE: "world_resource_packs.json",
}
SERVER_DIR = {
    BEHAVIOR: "behavior_packs",
    RESOURCE: "resource_packs",
}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

class Abort(Exception):
    """Fatal, user-facing error."""


def say(msg: str = "") -> None:
    try:
        print(msg, flush=True)
    except BrokenPipeError:
        # output was piped into something that closed early (head, more, ...)
        raise SystemExit(0)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_name(name: str) -> str:
    """Filesystem-safe folder name derived from a pack name."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")
    return cleaned[:60] or "pack"


def read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise Abort(f"Could not read {path}: {exc}") from exc
    if not text.strip():
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise Abort(f"{path} is not valid JSON ({exc}). Fix or delete it and retry.")


def write_json(path: Path, data) -> None:
    atomic_json(path, data)


def backup_file(path: Path) -> None:
    if not path.exists():
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(path, path.with_name(f"{path.name}.{stamp}.bak"))


def launched_by_doubleclick() -> bool:
    """True when Windows spawned a fresh console just for us (Explorer launch).

    GetConsoleProcessList reports how many processes share this console.
    From a cmd/PowerShell prompt that's >= 2 (the shell plus us); from a
    double-click it's 1, because we own the window and it dies with us.
    """
    if os.environ.get("BEDROCK_ADDONS_MENU"):
        return True
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        buf = (wintypes.DWORD * 8)()
        count = ctypes.windll.kernel32.GetConsoleProcessList(buf, 8)
        return count <= 1
    except Exception:
        return False


def hold(message: str = "Press Enter to close this window...") -> None:
    """Keep a double-clicked console window open so the output can be read."""
    try:
        input(f"\n{message}")
    except (EOFError, KeyboardInterrupt):
        pass


def server_is_running() -> bool:
    """Best-effort check so we don't edit files under a live server."""
    try:
        if sys.platform == "win32":
            from bedrock_metrics import windows_snapshot
            return windows_snapshot()['found']
        out = subprocess.run(["pgrep", "-f", "bedrock_server"],
                             capture_output=True, text=True, timeout=10)
        return out.returncode == 0
    except Exception:
        return True


# --------------------------------------------------------------------------
# server layout
# --------------------------------------------------------------------------

class Server:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.props = self.root / "server.properties"
        if not self.props.exists():
            raise Abort(
                f"No server.properties in {self.root}\n"
                "Put this script in your bedrock-server folder, or pass --server <path>."
            )
        self.level_name = self._read_level_name()
        self.world = world_path(self.root, self.level_name)
        if not self.world.exists():
            raise Abort(
                f"World folder not found: {self.world}\n"
                f"level-name in server.properties is '{self.level_name}'. "
                "Start the server once to generate the world, or fix level-name."
            )
        self.mods = self.root / "mods"
        self.mods.mkdir(exist_ok=True)
        self.state_path = self.mods / STATE_FILENAME

    def _read_level_name(self) -> str:
        for line in self.props.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == "level-name":
                return value.strip()
        raise Abort("level-name not found in server.properties")

    def pack_dir(self, kind: str) -> Path:
        d = self.root / SERVER_DIR[kind]
        d.mkdir(exist_ok=True)
        return d

    def world_json(self, kind: str) -> Path:
        return self.world / WORLD_JSON[kind]

    # ---- state ---------------------------------------------------------

    def load_state(self) -> dict:
        state = read_json(self.state_path, {})
        return state if isinstance(state, dict) else {}

    def save_state(self, state: dict) -> None:
        write_json(self.state_path, state)


# --------------------------------------------------------------------------
# manifest handling
# --------------------------------------------------------------------------

def normalize_version(raw) -> list[int]:
    """header.version may be [1,0,0] or '1.0.0'. Normalize to 3 ints."""
    if isinstance(raw, str):
        parts = re.split(r"[.\-+]", raw)
    elif isinstance(raw, (list, tuple)):
        parts = raw
    else:
        parts = []
    nums: list[int] = []
    for p in parts:
        try:
            nums.append(int(p))
        except (TypeError, ValueError):
            break
    while len(nums) < 3:
        nums.append(0)
    return nums[:3]


def classify(manifest: dict) -> str | None:
    """Behavior pack or resource pack, from module types."""
    types = {
        str(m.get("type", "")).lower()
        for m in (manifest.get("modules") or [])
        if isinstance(m, dict)
    }
    if types & {"resources"}:
        return RESOURCE
    if types & {"data", "script", "client_data", "javascript"}:
        return BEHAVIOR
    return None


def discover_packs(root: Path) -> list[dict]:
    """Find every pack in an extracted tree. Returns dicts with dir/uuid/etc."""
    found: list[dict] = []
    seen_uuids: set[str] = set()

    for manifest_path in sorted(root.rglob("manifest.json")):
        parts = {p.lower() for p in manifest_path.parts}
        # subpacks carry their own manifests but are not separately installable
        if "subpacks" in parts or "__macosx" in parts:
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(manifest, dict):
            continue
        header = manifest.get("header")
        if not isinstance(header, dict):
            continue
        uuid = header.get("uuid")
        if not isinstance(uuid, str) or not uuid.strip():
            continue
        uuid = uuid.strip()
        try:
            uuid = str(uuidlib.UUID(uuid))
        except ValueError:
            continue
        if uuid in seen_uuids:
            raise Abort(f'Duplicate pack UUID in archive: {uuid}')
        kind = classify(manifest)
        if kind is None:
            continue
        seen_uuids.add(uuid)
        name = header.get("name") or manifest_path.parent.name
        if isinstance(name, str) and name.startswith("pack."):
            name = manifest_path.parent.name  # localized key, not a real name
        found.append({
            "dir": manifest_path.parent,
            "uuid": uuid,
            "version": normalize_version(header.get("version")),
            "kind": kind,
            "name": str(name),
        })
    return found


def extract_archive(archive: Path, dest: Path, _depth: int = 0, _budget=None) -> None:
    """Extract a .mcaddon/.mcpack, recursing into nested pack archives."""
    if not zipfile.is_zipfile(archive):
        raise Abort(f"{archive.name} is not a valid zip archive (is it corrupt?)")
    if _depth > 5:
        raise Abort('Addon contains too many nested archives (maximum depth: 5).')
    if _budget is None:
        _budget = [2 * 1024**3, 50000]
    try:
        with zipfile.ZipFile(archive) as zf:
            _budget[0] -= sum(m.file_size for m in zf.infolist())
            _budget[1] -= len(zf.infolist())
            if min(_budget) < 0:
                raise Abort('Nested addon exceeds the total extraction budget.')
            extract_zip(zf, dest, max_bytes=2 * 1024**3, max_files=50000)
    except (ValueError, zipfile.BadZipFile, RuntimeError) as exc:
        raise Abort(f'{archive.name}: {exc}') from exc

    for nested in list(dest.rglob("*")):
        if nested.is_file() and nested.suffix.lower() in {".mcpack", ".mcaddon"}:
            sub = nested.with_suffix("")
            sub.mkdir(exist_ok=True)
            extract_archive(nested, sub, _depth + 1, _budget)
            nested.unlink()


# --------------------------------------------------------------------------
# world registration
# --------------------------------------------------------------------------

def register(server: Server, kind: str, packs: list[dict], dry: bool) -> None:
    path = server.world_json(kind)
    entries = read_json(path, [])
    if not isinstance(entries, list):
        raise Abort(f"{path} should contain a JSON array. Fix or delete it and retry.")

    changed = False
    for pack in packs:
        existing = next((e for e in entries
                         if isinstance(e, dict) and e.get("pack_id") == pack["uuid"]), None)
        if existing is None:
            entries.append({"pack_id": pack["uuid"], "version": pack["version"]})
            changed = True
        elif existing.get("version") != pack["version"]:
            existing["version"] = pack["version"]
            changed = True

    if changed and not dry:
        backup_file(path)
        write_json(path, entries)


def deregister(server: Server, kind: str, uuids: set[str], dry: bool) -> None:
    path = server.world_json(kind)
    entries = read_json(path, [])
    if not isinstance(entries, list):
        return
    kept = [e for e in entries
            if not (isinstance(e, dict) and e.get("pack_id") in uuids)]
    if len(kept) != len(entries) and not dry:
        backup_file(path)
        write_json(path, kept)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_install(server: Server, args) -> int:
    state = server.load_state()
    archives = sorted(
        p for p in server.mods.iterdir()
        if p.is_file() and p.suffix.lower() in ARCHIVE_SUFFIXES
    )
    if not archives:
        say(f"No .mcaddon or .mcpack files in {server.mods}")
        return 0

    installed = skipped = failed = 0

    for archive in archives:
        key = archive.name
        digest = sha256_file(archive)
        record = state.get(key)

        if record and record.get("disabled") and not args.force:
            if record.get("sha256") == digest:
                say(f"  disabled {key}  (uninstalled - 'enable {Path(key).stem}' to bring it back)")
                skipped += 1
                continue
            # The archive changed since it was disabled, so this is a new drop
            # or an update - honour that rather than the old uninstall.
            say(f"  updated  {key}  (was disabled, but the file changed - installing)")
            record = None

        if record and record.get("sha256") == digest and not args.force:
            missing = [p for p in record.get("packs", [])
                       if not (server.root / SERVER_DIR[p["kind"]] / p["folder"]).exists()]
            if not missing:
                say(f"  skip     {key}  (already installed)")
                skipped += 1
                continue
            say(f"  repair   {key}  ({len(missing)} pack folder(s) missing)")

        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmpdir = Path(tmp)
                extract_archive(archive, tmpdir)
                packs = discover_packs(tmpdir)
                if not packs:
                    say(f"  FAIL     {key}  (no valid manifest.json found inside)")
                    failed += 1
                    continue
                from bedrock_mods import install_packs
                recorded = install_packs(server, packs, record, state, key, digest, args.dry_run)
                for pack in recorded:
                    vs = ".".join(str(n) for n in pack["version"])
                    say(f"  install  {key}  ->  [{pack['kind']}] {pack['name']} v{vs}")
                installed += 1
        except (Abort, OSError, ValueError, RuntimeError) as exc:
            say(f"  FAIL     {key}  ({exc})")
            failed += 1

    say()
    if args.dry_run:
        say("[dry-run] Nothing was changed.")
    say(f"installed {installed} | skipped {skipped} | failed {failed}")
    if installed and not args.dry_run:
        say("Restart the server for changes to take effect.")
    return 1 if failed else 0


def remove_packs(server: Server, record: dict, dry: bool) -> None:
    # Validate every target before changing registrations or deleting anything.
    for pack in record.get('packs', []):
        base = server.root / SERVER_DIR[pack['kind']]
        folder = base / pack['folder']
        if (not pack['folder'] or folder.is_symlink() or
                folder.resolve().parent != base.resolve() or
                Path(pack['folder']).name != pack['folder']):
            raise Abort(f"Unsafe recorded pack folder: {pack['folder']}")
    for kind in (BEHAVIOR, RESOURCE):
        uuids = {p["uuid"] for p in record.get("packs", []) if p["kind"] == kind}
        if uuids:
            deregister(server, kind, uuids, dry=dry)
    for pack in record.get("packs", []):
        folder = server.root / SERVER_DIR[pack["kind"]] / pack["folder"]
        if folder.exists() and not dry:
            shutil.rmtree(folder, ignore_errors=True)
            if folder.exists():
                say(f"           (could not delete {pack['folder']} - it is no "
                    "longer enabled in the world, but the files remain)")


def cmd_uninstall(server: Server, args) -> int:
    state = server.load_state()
    if not state:
        say("Nothing is installed.")
        return 0

    if args.all:
        targets = list(state.keys())
    else:
        if not args.name:
            say("Give a name, or use --all. Run 'list' to see installed addons.")
            return 1
        needle = args.name.lower().removesuffix(".mcaddon").removesuffix(".mcpack")
        targets = [k for k in state
                   if needle in k.lower() or needle in Path(k).stem.lower()]
        if not targets:
            say(f"No installed addon matching '{args.name}'. Run 'list' to see them.")
            return 1
        if len(targets) > 1:
            say(f"'{args.name}' matches several addons:")
            for t in targets:
                say(f"  {t}")
            say("Be more specific.")
            return 1

    for key in targets:
        record = state[key]
        for pack in record.get("packs", []):
            say(f"  remove   {key}  ->  [{pack['kind']}] {pack['name']}")
        if not record.get("packs"):
            say(f"  remove   {key}  (no packs recorded)")
        remove_packs(server, record, dry=args.dry_run)
        if args.dry_run:
            continue
        if args.purge:
            archive = server.mods / key
            if archive.exists():
                archive.unlink()
                say(f"  deleted  {archive.name} from mods/")
            del state[key]
        else:
            # keep the archive in mods/ but remember not to reinstall it
            state[key] = {
                "sha256": record.get("sha256"),
                "disabled": True,
                "uninstalled_at": datetime.now().isoformat(timespec="seconds"),
                "packs": [],
                "last_packs": record.get("packs", []),
            }

    if not args.dry_run:
        server.save_state(state)

    say()
    if args.dry_run:
        say(f"[dry-run] Nothing was changed. {len(targets)} addon(s) would be removed.")
        return 0
    if args.purge:
        say(f"Removed {len(targets)} addon(s) and deleted the archive(s) from mods/.")
    else:
        say(f"Removed {len(targets)} addon(s). The archive(s) stay in mods/ but are")
        say("marked disabled, so 'install' will leave them alone.")
        say("Run 'enable NAME' to put one back.")
    say("Restart the server for changes to take effect.")
    return 0


def cmd_enable(server: Server, args) -> int:
    state = server.load_state()
    needle = args.name.lower().removesuffix(".mcaddon").removesuffix(".mcpack")
    targets = [k for k, v in state.items()
               if v.get("disabled") and (needle in k.lower() or needle in Path(k).stem.lower())]
    if not targets:
        say(f"No disabled addon matching '{args.name}'. Run 'list' to see them.")
        return 1
    for key in targets:
        if not args.dry_run:
            del state[key]
        say(f"  enabled  {key}")
    if not args.dry_run:
        server.save_state(state)
    say()
    say("Run 'install' to put it back on the server.")
    return 0


def cmd_list(server: Server, args) -> int:
    state = server.load_state()
    archives = {p.name for p in server.mods.iterdir()
                if p.is_file() and p.suffix.lower() in ARCHIVE_SUFFIXES}

    say(f"Server : {server.root}")
    say(f"World  : {server.level_name}")
    say(f"Mods   : {server.mods}")
    say()

    live = {k: v for k, v in state.items() if not v.get("disabled")}
    dead = {k: v for k, v in state.items() if v.get("disabled")}

    if not live:
        say("Installed: (none)")
    else:
        say("Installed:")
        for key, record in sorted(live.items()):
            when = record.get("installed_at", "?")
            flag = "" if key in archives else "   [archive missing from mods/]"
            say(f"  {key}{flag}")
            say(f"    added {when}")
            for pack in record.get("packs", []):
                vs = ".".join(str(n) for n in pack["version"])
                folder = server.root / SERVER_DIR[pack["kind"]] / pack["folder"]
                mark = "ok" if folder.exists() else "MISSING"
                say(f"    [{pack['kind']:8}] {pack['name']} v{vs}  ({mark})")

    if dead:
        say()
        say("Disabled (archive kept in mods/, will not reinstall):")
        for key, record in sorted(dead.items()):
            say(f"  {key}   uninstalled {record.get('uninstalled_at', '?')}")

    pending = sorted(archives - set(state.keys()))
    say()
    if pending:
        say("Not yet installed (run 'install'):")
        for name in pending:
            say(f"  {name}")
    else:
        say("Not yet installed: (none)")
    return 0


def cmd_verify(server: Server, args) -> int:
    state = server.load_state()
    problems = 0
    # packs switched off from the launcher are absent on purpose
    deliberate = read_json(server.mods / "world_disabled.json", [])
    deliberate = set(deliberate) if isinstance(deliberate, list) else set()
    for key, record in sorted(state.items()):
        for pack in record.get("packs", []):
            folder = server.root / SERVER_DIR[pack["kind"]] / pack["folder"]
            if not folder.exists():
                say(f"  MISSING FOLDER  {key} -> {pack['name']}")
                problems += 1
        for kind in (BEHAVIOR, RESOURCE):
            uuids = {p["uuid"] for p in record.get("packs", []) if p["kind"] == kind}
            if not uuids:
                continue
            entries = read_json(server.world_json(kind), [])
            listed = {e.get("pack_id") for e in entries if isinstance(e, dict)}
            for missing in uuids - listed:
                if missing in deliberate:
                    say(f"  disabled on purpose  {key} -> {missing}")
                    continue
                say(f"  NOT REGISTERED  {key} -> {missing} in {WORLD_JSON[kind]}")
                problems += 1
    if problems:
        say()
        say(f"{problems} problem(s). Run 'install --force' to rebuild.")
        return 1
    say("All installed addons look intact.")
    return 0


# --------------------------------------------------------------------------

MENU = """
================================================
  Bedrock Addon Manager
================================================
  1) Install new addons from mods/
  2) List addons (installed / disabled / pending)
  3) Uninstall an addon
  4) Re-enable a disabled addon
  5) Verify installed addons are intact
  6) Preview an install (dry run, changes nothing)
  0) Quit
================================================"""


def _ns(**kw):
    """Build the args object the cmd_* handlers expect."""
    base = dict(dry_run=False, yes=False, force=False, all=False,
                purge=False, name=None)
    base.update(kw)
    return argparse.Namespace(**base)


def _pick_addon(server: Server, disabled: bool) -> str | None:
    """Show a numbered list and let the user choose one."""
    state = server.load_state()
    keys = sorted(k for k, v in state.items()
                  if bool(v.get("disabled")) == disabled)
    if not keys:
        say("  (nothing to choose from)")
        return None
    say()
    for i, k in enumerate(keys, 1):
        say(f"  {i}) {k}")
    say("  0) cancel")
    raw = input("\nNumber: ").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= len(keys)):
        return None
    return keys[int(raw) - 1]  # menu is 1-based, list is 0-based


def run_menu(server: Server) -> int:
    say(f"Server : {server.root}")
    say(f"World  : {server.level_name}")
    say(f"Mods   : {server.mods}")

    while True:
        say(MENU)
        try:
            choice = input("Choose: ").strip()
        except (EOFError, KeyboardInterrupt):
            say()
            return 0

        say()
        try:
            if choice == "1":
                cmd_install(server, _ns())
            elif choice == "2":
                cmd_list(server, _ns())
            elif choice == "3":
                target = _pick_addon(server, disabled=False)
                if target:
                    say()
                    cmd_uninstall(server, _ns(name=target))
            elif choice == "4":
                target = _pick_addon(server, disabled=True)
                if target:
                    say()
                    cmd_enable(server, _ns(name=target))
            elif choice == "5":
                cmd_verify(server, _ns())
            elif choice == "6":
                cmd_install(server, _ns(dry_run=True))
            elif choice == "0":
                return 0
            else:
                say("  Not an option.")
        except Abort as exc:
            say(f"  ERROR: {exc}")
        except KeyboardInterrupt:
            say("\n  Cancelled.")

        try:
            input("\nPress Enter for the menu...")
        except (EOFError, KeyboardInterrupt):
            return 0


def main() -> int:
    # global flags live on a parent parser so they work before OR after
    # the subcommand - "install --yes" and "--yes install" both parse.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--server", type=Path, default=argparse.SUPPRESS,
                        help="Path to the bedrock-server directory (default: this script's folder)")
    common.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS,
                        help="Show what would happen, change nothing")
    common.add_argument("--yes", action="store_true", default=argparse.SUPPRESS,
                        help="Noninteractive mode; the server must still be stopped")

    parser = argparse.ArgumentParser(
        parents=[common],
        description="Install and remove Bedrock addons from a mods/ drop folder.")

    sub = parser.add_subparsers(dest="command")
    ins = sub.add_parser("install", parents=[common], help="Install anything new in mods/")
    ins.add_argument("--force", action="store_true", help="Reinstall even if already installed")
    un = sub.add_parser("uninstall", parents=[common], help="Remove an installed addon")
    un.add_argument("name", nargs="?", help="Archive name or part of it")
    un.add_argument("--all", action="store_true", help="Remove every installed addon")
    un.add_argument("--purge", action="store_true",
                    help="Also delete the archive from mods/ (default: keep it, marked disabled)")
    en = sub.add_parser("enable", parents=[common],
                        help="Un-disable an addon so 'install' will pick it up again")
    en.add_argument("name", help="Archive name or part of it")
    sub.add_parser("list", parents=[common], help="Show installed and pending addons")
    sub.add_parser("verify", parents=[common], help="Check installed packs are still intact")

    args = parser.parse_args()
    args.server = getattr(args, 'server', Path(__file__).resolve().parent)
    args.dry_run = getattr(args, 'dry_run', False)
    args.yes = getattr(args, 'yes', False)
    if not args.command:
        args.command = "install"
        args.force = False

    # No arguments + our own console window == double-clicked in Explorer.
    interactive = len(sys.argv) == 1 and launched_by_doubleclick()

    try:
        server = Server(args.server)
    except Abort as exc:
        say(f"ERROR: {exc}")
        if interactive:
            hold()
        return 2

    if interactive:
        code = run_menu(server)
        return code

    if args.command in {"install", "uninstall"} and not args.dry_run:
        if server_is_running():
            say('Stop the server before editing packs. --yes does not bypass this check.')
            return 1

    handlers = {
        "install": cmd_install,
        "uninstall": cmd_uninstall,
        "enable": cmd_enable,
        "list": cmd_list,
        "verify": cmd_verify,
    }
    try:
        if args.command in {'install', 'uninstall', 'enable'} and not args.dry_run:
            with operation_lock(server.root):
                return handlers[args.command](server, args)
        return handlers[args.command](server, args)
    except (Abort, RuntimeError) as exc:
        say(f"ERROR: {exc}")
        return 2
    except KeyboardInterrupt:
        say("\nInterrupted.")
        return 130


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        if launched_by_doubleclick():
            hold("Something went wrong. Press Enter to close...")
        sys.exit(2)
