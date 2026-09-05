#!/usr/bin/env python3
"""
bedrock_update.py - Bedrock Dedicated Server updater and version picker.

Stdlib only, like everything else here.

Where versions come from
------------------------
Mojang publish a small JSON document listing the current downloads. That is
the authority for "what is the latest version" - no scraping, no HTML, no
bot-check to get around:

    https://net-secondary.web.minecraft-services.net/api/v1.0/download/links

Any *specific* version comes from the archive URL, which is predictable:

    https://www.minecraft.net/bedrockdedicatedserver/bin-win/bedrock-server-<v>.zip

Most versions are still up - 1.16.201.02 still resolves - but there are gaps,
so a version is always confirmed with a HEAD before anything is downloaded.

How the update is applied
-------------------------
In place. The new build is extracted over the server folder and everything
that is yours is left exactly where it is:

  - worlds/                 backed up, never overwritten by the update
  - server.properties       kept, along with allowlist and permissions
  - custom behaviour packs  survive because extraction only ever *writes*
                            files the zip contains; nothing is deleted

The folder name stops being true the moment you update, so the real version
is recorded in bedrock_version.json instead. Read installed_version() rather
than parsing the directory name.

Downgrades are allowed but they are not safe: Bedrock upgrades a world's
format the first time a newer build opens it, and older builds then refuse
it. apply_update() always backs the world up first for exactly this reason.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
from bedrock_storage import atomic_json, create_backup, operation_lock, zip_members

LINKS_API = ("https://net-secondary.web.minecraft-services.net"
             "/api/v1.0/download/links")
ARCHIVE_URL = ("https://www.minecraft.net/bedrockdedicatedserver/bin-win/"
               "bedrock-server-{version}.zip")

# minecraft.net serves the archive to a browser user-agent; the urllib
# default gets a 403.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

VERSION_FILE = "bedrock_version.json"
DOWNLOAD_DIR = "downloads"
IS_WIN = sys.platform == "win32"

RE_VERSION = re.compile(r"^\d+(?:\.\d+){2,3}$")
RE_FOLDER_VERSION = re.compile(r"bedrock-server-(\d+(?:\.\d+){2,3})")

# Files the zip ships that are yours the moment the server has run once.
# Extraction skips them; without this an update silently resets the server
# name, the difficulty, and content-log-console-output-enabled, which the
# in-game menu depends on.
PRESERVE_FILES = frozenset({
    "server.properties",
    "allowlist.json",
    "whitelist.json",
    "permissions.json",
})

# Nothing under these is ever written, at any depth.
PRESERVE_TREES = ("worlds/",)


class UpdateError(RuntimeError):
    """Anything that should stop an update, phrased for a person to read."""


# ---------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------

def parse_version(text: str) -> tuple:
    """'1.26.45.1' -> (1, 26, 45, 1). Unparseable sorts lowest."""
    try:
        return tuple(int(p) for p in str(text).strip().split("."))
    except (ValueError, AttributeError):
        return ()


def compare_versions(a: str, b: str) -> int:
    """-1 if a < b, 0 if equal, 1 if a > b. Pads so 1.26.45 == 1.26.45.0."""
    pa, pb = parse_version(a), parse_version(b)
    width = max(len(pa), len(pb))
    pa += (0,) * (width - len(pa))
    pb += (0,) * (width - len(pb))
    return (pa > pb) - (pa < pb)


def valid_version(text: str) -> bool:
    return bool(RE_VERSION.match(str(text or "").strip()))


def version_url(version: str) -> str:
    return ARCHIVE_URL.format(version=str(version).strip())


def _open(url: str, method: str = "GET", timeout: int = 25):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "*/*"},
                                 method=method)
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_available(timeout: int = 25) -> dict:
    """Ask Mojang what is current. Returns {'stable':..., 'preview':...}."""
    try:
        with _open(LINKS_API, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise UpdateError(f"the version list returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise UpdateError(f"could not reach the version list ({exc.reason})") from exc
    except (ValueError, TypeError) as exc:
        raise UpdateError("the version list was not readable JSON") from exc

    wanted = {"serverBedrockWindows": "stable",
              "serverBedrockPreviewWindows": "preview"}
    out: dict = {}
    for link in (payload.get("result") or {}).get("links") or []:
        key = wanted.get(link.get("downloadType"))
        if not key:
            continue
        url = link.get("downloadUrl") or ""
        found = RE_FOLDER_VERSION.search(url)
        if found:
            out[key] = found.group(1)
            out[key + "_url"] = url
    if "stable" not in out:
        raise UpdateError("the version list had no Windows server build in it")
    return out


def check_version(version: str, timeout: int = 20) -> tuple[str, int]:
    """Does this build exist? Returns (state, size_bytes).

    state is "available", "missing", or "blocked". The distinction matters:
    the download server rate-limits, and a 403 means "ask again later", not
    "this version was never released". Treating the two the same makes the
    tool confidently wrong about which versions exist.
    """
    if not valid_version(version):
        return "missing", 0
    try:
        with _open(version_url(version), method="HEAD", timeout=timeout) as resp:
            return "available", int(resp.headers.get("Content-Length") or 0)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return "missing", 0
        return "blocked", 0
    except urllib.error.URLError:
        return "blocked", 0


def installed_version(root: Path) -> str:
    """What is actually installed. The marker first, the folder name second."""
    marker = Path(root) / VERSION_FILE
    try:
        data = json.loads(marker.read_text(encoding="utf-8-sig"))
        version = str(data.get("version") or "").strip()
        if valid_version(version):
            return version
    except (OSError, ValueError, AttributeError):
        pass
    found = RE_FOLDER_VERSION.search(Path(root).resolve().name)
    return found.group(1) if found else ""


def write_marker(root: Path, version: str, source: str = "update") -> None:
    payload = {
        "version": version,
        "source": source,
        "installed_at": datetime.now().replace(microsecond=0).isoformat(sep=" "),
        "note": ("The folder name is not the version. This file is. "
                 "Written by bedrock_update.py."),
    }
    try:
        atomic_json(Path(root) / VERSION_FILE, payload)
    except OSError as exc:
        raise UpdateError(f"could not record the installed version: {exc}")


def server_running() -> bool:
    """True if bedrock_server.exe is alive - ours or anyone else's."""
    try:
        if IS_WIN:
            from bedrock_metrics import windows_snapshot
            return windows_snapshot()['found']
        out = subprocess.run(["pgrep", "-f", "bedrock_server"],
                             capture_output=True, text=True, timeout=15)
        return bool(out.stdout.strip())
    except Exception:
        # If we cannot tell, say yes. Refusing to update is recoverable;
        # extracting over a running server is not.
        return True


# ---------------------------------------------------------------------------
# which builds exist
#
# There is no endpoint that lists every release - the links API reports only
# the current stable and preview. The obvious workaround, probing the archive
# URL for every plausible version, does not work: a few thousand HEADs in
# quick succession gets the IP 403'd for a while, and then every lookup lies
# about what exists. So there is deliberately no scanner here.
#
# Instead the picker is built from sources that cost at most one request:
#
#   KNOWN_BUILDS   versions confirmed by hand to return 200. Not exhaustive,
#                  and not guessed - each one was really checked.
#   the links API  the current stable and preview, one request
#   downloads/     anything already fetched, free
#   the cache      versions the user has checked before, remembered so the
#                  same lookup is not repeated
#
# Anything not listed can still be typed in and checked individually.
# ---------------------------------------------------------------------------

CACHE_FILE = "versions_cache.json"

# Confirmed to return 200 from the download server. Keep this honest: only
# add a version after it has actually been checked.
KNOWN_BUILDS = (
    "1.16.201.02",
    "1.19.20.02", "1.19.21.01", "1.19.22.01", "1.19.30.04", "1.19.80.02",
    "1.20.81.01",
    "1.21.44.01",
    "1.26.0.2", "1.26.1.1", "1.26.2.1", "1.26.3.1",
    "1.26.10.4", "1.26.11.1", "1.26.12.2", "1.26.13.1",
    "1.26.42.1", "1.26.43.1", "1.26.44.3", "1.26.45.1",
)


def family_of(version: str) -> str:
    """'1.26.45.1' -> '1.26'."""
    parts = str(version).split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else str(version)


def sort_versions(versions, newest_first: bool = True) -> list:
    return sorted(versions, key=parse_version, reverse=newest_first)


def load_cache(root: Path) -> dict:
    """{version: size_bytes} for builds already confirmed to exist."""
    try:
        data = json.loads((Path(root) / CACHE_FILE).read_text(encoding="utf-8-sig"))
        versions = data.get("versions")
        if isinstance(versions, dict):
            return {str(k): int(v or 0) for k, v in versions.items()
                    if valid_version(k)}
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return {}


def remember_version(root: Path, version: str, size: int) -> None:
    """Add a confirmed build to the cache so it shows up in the list."""
    if not valid_version(version):
        return
    known = load_cache(root)
    known[version] = int(size or 0)
    payload = {
        "checked_at": datetime.now().replace(microsecond=0).isoformat(sep=" "),
        "note": ("Builds confirmed to exist, one lookup at a time. "
                 "Safe to delete; it will refill as versions are checked."),
        "versions": {v: known[v] for v in sort_versions(known)},
    }
    try:
        atomic_json(Path(root) / CACHE_FILE, payload)
    except OSError:
        pass


def catalogue(root: Path, available: dict | None = None) -> list:
    """Everything the picker should show, newest first.

    Merges the known-good list, the cache, whatever the links API reported,
    the installed build, and any zip already on disk. Each row carries what
    the view needs, so rendering never touches the network.
    """
    root = Path(root)
    sizes = {v: 0 for v in KNOWN_BUILDS}
    sizes.update(load_cache(root))

    installed = installed_version(root)
    channels = {}
    if available:
        for key in ("stable", "preview"):
            version = available.get(key)
            if version:
                sizes.setdefault(version, 0)
                channels[version] = key

    on_disk = set()
    for folder in (root / DOWNLOAD_DIR, root):
        try:
            for path in folder.glob("bedrock-server-*.zip"):
                found = RE_FOLDER_VERSION.search(path.name)
                if found:
                    on_disk.add(found.group(1))
                    if not sizes.get(found.group(1)):
                        sizes[found.group(1)] = path.stat().st_size
        except OSError:
            pass

    if installed:
        sizes.setdefault(installed, 0)

    rows = []
    for version in sort_versions(sizes):
        rows.append({
            "version": version,
            "size": sizes.get(version, 0),
            "channel": channels.get(version, "archive"),
            "installed": version == installed,
            "downloaded": version in on_disk,
            "family": family_of(version),
            "relation": (0 if not installed
                         else compare_versions(version, installed)),
        })
    return rows


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------

def download(version: str, root: Path, progress=None,
             reuse: bool = True) -> Path:
    """Fetch a version's zip into downloads/. Returns the file.

    progress(done_bytes, total_bytes, label) is called as it streams.
    An already-downloaded zip that opens cleanly is reused rather than
    pulled again - 91 MB is worth not repeating.
    """
    root = Path(root)
    if not valid_version(version):
        raise UpdateError(f"{version!r} does not look like a version number")

    dest_dir = root / DOWNLOAD_DIR
    dest_dir.mkdir(exist_ok=True)
    dest = dest_dir / f"bedrock-server-{version}.zip"

    if reuse:
        for candidate in (dest, root / f"bedrock-server-{version}.zip"):
            if candidate.is_file() and _zip_ok(candidate):
                if progress:
                    size = candidate.stat().st_size
                    progress(size, size, f"using {candidate.name} already on disk")
                return candidate

    url = version_url(version)
    part = dest.with_suffix(".zip.part")
    try:
        with _open(url, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            last = 0.0
            with open(part, "wb") as handle:
                while True:
                    chunk = resp.read(262144)
                    if not chunk:
                        break
                    handle.write(chunk)
                    done += len(chunk)
                    now = time.time()
                    if progress and (now - last > 0.15 or done == total):
                        last = now
                        progress(done, total, f"downloading {version}")
    except urllib.error.HTTPError as exc:
        part.unlink(missing_ok=True)
        if exc.code == 404:
            raise UpdateError(f"there is no build {version} on the download "
                              "server") from exc
        raise UpdateError(f"download failed with HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        part.unlink(missing_ok=True)
        raise UpdateError(f"download failed ({exc.reason})") from exc
    except OSError as exc:
        part.unlink(missing_ok=True)
        raise UpdateError(f"could not write the download: {exc}") from exc
    except Exception:
        part.unlink(missing_ok=True)
        raise

    if not _zip_ok(part):
        part.unlink(missing_ok=True)
        raise UpdateError("the download arrived damaged - try again")

    part.replace(dest)
    return dest


def _zip_ok(path: Path) -> bool:
    """A readable zip that actually contains a server."""
    try:
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                return False
            names = archive.namelist()
        return 'bedrock_server.exe' in names
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------

def _skip(name: str) -> bool:
    """True for archive members that must not overwrite what is here."""
    clean = name.replace("\\", "/").lstrip("/")
    if not clean or clean.endswith("/"):
        return False
    if clean.lower() in PRESERVE_FILES:
        return True
    low = clean.lower()
    return any(low.startswith(tree) for tree in PRESERVE_TREES)


def backup_world(root: Path, progress=None) -> Path | None:
    """Zip the world before touching anything. Returns the archive."""
    root = Path(root)
    level = "Bedrock level"
    try:
        for line in (root / "server.properties").read_text(
                encoding="utf-8-sig", errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            if key.strip() == "level-name":
                level = value.strip() or level
                break
    except OSError:
        pass

    world = root / "worlds" / level
    if not world.is_dir():
        return None
    dest = root / "backups"
    dest.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = dest / f"{level}-preupdate-{stamp}"
    if progress:
        progress(0, 0, f"backing up {level}")
    return create_backup(root, 'preupdate', progress=progress)


def apply_update(root: Path, zip_path: Path, version: str,
                 progress=None, backup: bool = True) -> dict:
    with operation_lock(root):
        return _apply_update_locked(root, zip_path, version, progress, backup)


def _apply_update_locked(root: Path, zip_path: Path, version: str,
                         progress=None, backup: bool = True) -> dict:
    """Extract a downloaded build over the server folder, in place.

    Refuses to run while bedrock_server.exe is alive. Backs the world up
    first unless told not to. Never writes anything in PRESERVE_FILES or
    under worlds/, and never deletes - so custom packs and this tooling
    come through untouched.
    """
    root = Path(root)
    zip_path = Path(zip_path)
    if not valid_version(version):
        raise UpdateError('Enter a valid server version.')

    if server_running():
        raise UpdateError("bedrock_server.exe is running - stop the server "
                          "before updating")
    if not _zip_ok(zip_path):
        raise UpdateError(f"{zip_path.name} is not a usable server archive")

    previous = installed_version(root)
    report = {
        "from": previous,
        "to": version,
        "downgrade": bool(previous and compare_versions(version, previous) < 0),
        "backup": None,
        "written": 0,
        "skipped": 0,
        "bytes": 0,
    }

    # Reject every unsafe entry before backing up or replacing even one file.
    with zipfile.ZipFile(zip_path) as archive:
        zip_members(archive, root)

    from bedrock_recovery import Transaction, create_restore_point, assert_recovered
    assert_recovered(root)
    if backup:
        made = create_restore_point(root, 'Before server update ' + version, progress)
        report["backup"] = str(made) if made else None

    stage = Path(tempfile.mkdtemp(prefix='.update-stage-', dir=root))
    transaction = None
    try:
        with zipfile.ZipFile(zip_path) as archive:
            members = [m for m in archive.infolist() if not m.is_dir()]
            wanted = [m for m in members if not _skip(m.filename.replace('\\', '/'))]
            required = sum(m.file_size for m in wanted) * 2
            required += sum((root / m.filename).stat().st_size for m in wanted if (root / m.filename).is_file())
            if shutil.disk_usage(root).free < required + 16 * 1024**2:
                raise UpdateError('Not enough disk space to stage the update and its recovery files.')
            for index, member in enumerate(members, 1):
                name = member.filename.replace('\\', '/')
                if _skip(name):
                    report['skipped'] += 1
                    continue
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as src, target.open('wb') as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                report['bytes'] += member.file_size
                if progress and (index % 40 == 0 or index == len(members)):
                    progress(index, len(members), f'Staging {version}')
        transaction = Transaction(root, f'Update server to {version}', progress)
        sources = sorted(p for p in stage.rglob('*') if p.is_file())
        for index, source in enumerate(sources, 1):
            relative = source.relative_to(stage)
            target = root / relative
            transaction.replace(target, source)
            report['written'] += 1
            if progress and (index % 40 == 0 or index == len(sources)):
                progress(index, len(sources), 'Preparing recovery snapshots')
        transaction.write_json(root / VERSION_FILE, {'version': version, 'source': zip_path.name,
                                                      'installed_at': datetime.now().isoformat(timespec='seconds')})
        transaction.commit()
    except Exception as exc:
        if transaction:
            transaction.cancel()
        raise UpdateError(f'Update failed: {exc}. Check Recovery for any unfinished operation.') from exc
    finally:
        shutil.rmtree(stage)
    return report


# ---------------------------------------------------------------------------
# standalone
# ---------------------------------------------------------------------------

def _human(n: float) -> str:
    return f"{n / 1048576:.0f} MB" if n else "?"


def main(argv: list[str]) -> int:
    root = Path(argv[1]).resolve() if len(argv) > 1 else Path(__file__).resolve().parent
    here = installed_version(root) or "unknown"
    print(f"Bedrock updater - {root}")
    print(f"  installed: {here}")

    try:
        available = fetch_available()
    except UpdateError as exc:
        print(f"  {exc}")
        return 1

    stable = available.get("stable", "")
    preview = available.get("preview", "")
    print(f"  stable   : {stable}", end="")
    if here != "unknown" and stable:
        gap = compare_versions(stable, here)
        print("  (update available)" if gap > 0 else
              "  (up to date)" if gap == 0 else "  (older than yours)")
    else:
        print()
    if preview:
        print(f"  preview  : {preview}")

    if len(argv) > 2 and argv[2] == "--install" and stable:
        target = argv[3] if len(argv) > 3 else stable
        state, size = check_version(target)
        if state != "available":
            print(f"  {target}: {state}"
                  + ("  (the download server is rate-limiting - wait a few "
                     "minutes)" if state == "blocked" else ""))
            return 1
        print(f"  fetching {target} ({_human(size)})")

        def show(done, total, label):
            if total:
                print(f"\r  {label}: {done * 100 // total}%", end="", flush=True)

        archive = download(target, root, show)
        print()
        result = apply_update(root, archive, target, show)
        print()
        print(f"  {result['written']} files written, "
              f"{result['skipped']} preserved")
        if result["backup"]:
            print(f"  world backed up to {Path(result['backup']).name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
