#!/usr/bin/env python3
"""
server_manager.py - supervise a Minecraft Bedrock dedicated server:
scheduled restarts with in-game warnings, and auto-restart on crash.

Put this next to bedrock_server.exe and run it INSTEAD of launching the
server directly. It starts the server as a child process, so it can send
console commands to it - which is how players get warned.

    python server_manager.py            # run the server, restart on schedule
    python server_manager.py --now      # trigger one restart cycle now (test)
    python server_manager.py --check    # print the schedule and exit

Type commands at the prompt and they go straight to the server console,
same as running it directly. Ctrl+C stops the server cleanly and exits.

Edit RESTART_TIMES and WARN_MINUTES below. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import logging
from logging.handlers import RotatingFileHandler
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from bedrock_storage import atomic_json, create_backup, prune_automatic, operation_lock

# ---------------------------------------------------------------------------
# CONFIGURATION - edit these
# ---------------------------------------------------------------------------

# Defaults, used only to create manager_config.json on first run.
# After that, edit the JSON file or change it in-game with
#   /scriptevent mgr:schedule 06:00,14:00,22:00
RESTART_TIMES = ["06:00", "14:00", "22:00"]

# Minutes before a restart to warn players, largest first.
WARN_MINUTES = [15, 10, 5, 2, 1]

# Where the live schedule is stored (next to this script).
CONFIG_FILE = "manager_config.json"

# Marker the companion behavior pack prints to the content log.
MGR_MARKER = re.compile(r"\[MGR\]\|([a-z_]+)\|(.*)$")

# scriptevent namespace the manager uses to talk back to the pack
NS_BACK = "mgrback"

# Also count down the final seconds on screen.
WARN_SECONDS = [30, 10, 5, 4, 3, 2, 1]

# How long to wait for a clean shutdown before force-killing.
STOP_TIMEOUT = 90

# Seconds to wait between the server exiting and starting it again.
RESTART_DELAY = 5

# Name of the server executable.
SERVER_EXE = "bedrock_server.exe" if sys.platform == "win32" else "bedrock_server"

# Write a copy of server output here (set to None to disable).
LOG_FILE = "server_manager.log"

# ---------------------------------------------------------------------------

COLOR_WARN = "§e"   # yellow
COLOR_URGENT = "§c"  # red
COLOR_OK = "§a"      # green


def now() -> datetime:
    return datetime.now()


def stamp() -> str:
    return now().strftime("%H:%M:%S")


def log(msg: str) -> None:
    print(f"[manager {stamp()}] {msg}", flush=True)


def load_config(path: Path) -> dict:
    defaults = {"restart_times": list(RESTART_TIMES),
                "warn_minutes": list(WARN_MINUTES),
                "backup_before_restart": False, "backup_keep": 10,
                "crash_restart_limit": 5}
    if not path.exists():
        save_config(path, defaults)
        return defaults
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError) as exc:
        log(f"WARNING: {path.name} unreadable ({exc}); using defaults")
        return defaults
    if not isinstance(data, dict):
        return defaults
    merged = {**defaults, **data}
    if isinstance(data.get("restart_times"), list) and data["restart_times"]:
        try:
            merged["restart_times"] = [f"{h:02d}:{m:02d}" for h, m in parse_times(
                [str(v) for v in data["restart_times"]])]
        except SystemExit:
            log('WARNING: invalid restart times; using defaults')
            merged['restart_times'] = defaults['restart_times']
    else:
        merged['restart_times'] = defaults['restart_times']
    if isinstance(data.get("warn_minutes"), list) and data["warn_minutes"]:
        try:
            merged["warn_minutes"] = sorted({int(v) for v in data["warn_minutes"]
                                              if 0 < int(v) <= 1440}, reverse=True)
        except (TypeError, ValueError):
            merged['warn_minutes'] = defaults['warn_minutes']
    else:
        merged['warn_minutes'] = defaults['warn_minutes']
    merged["backup_before_restart"] = data.get("backup_before_restart") is True
    for key, high in (('backup_keep', 1000), ('crash_restart_limit', 100)):
        try:
            merged[key] = max(1, min(high, int(data.get(key, defaults[key]))))
        except (TypeError, ValueError):
            merged[key] = defaults[key]
    return merged


def save_config(path: Path, config: dict) -> None:
    atomic_json(path, config)


def parse_times(values: list[str]) -> list[tuple[int, int]]:
    out = []
    for v in values:
        m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", v)
        if not m:
            raise SystemExit(f"Bad time: {v!r} - use HH:MM, 24-hour")
        h, mi = int(m.group(1)), int(m.group(2))
        if not (0 <= h <= 23 and 0 <= mi <= 59):
            raise SystemExit(f"Bad time: {v!r} - use HH:MM, 24-hour")
        out.append((h, mi))
    if not out:
        raise SystemExit("RESTART_TIMES is empty.")
    return sorted(set(out))


def next_restart(times: list[tuple[int, int]], after: datetime) -> datetime:
    """Earliest scheduled time strictly after `after`."""
    for day in (0, 1):
        base = (after + timedelta(days=day)).date()
        for h, mi in times:
            candidate = datetime.combine(base, datetime.min.time()).replace(hour=h, minute=mi)
            if candidate > after:
                return candidate
    raise RuntimeError("unreachable")


def human(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


# ---------------------------------------------------------------------------


class ServerProcess:
    """Wraps the bedrock_server child process."""

    def __init__(self, root: Path, log_path: Path | None, on_command=None, on_line=None, command=None):
        self.root = root
        self.on_command = on_command
        self.on_line = on_line
        self.command = command
        self.exe = root / SERVER_EXE
        if not self.exe.exists() and not command:
            raise SystemExit(f"{SERVER_EXE} not found in {root}")
        self.log_path = log_path
        self.proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self.alive():
            raise RuntimeError('The server is already running.')
        if self._reader:
            self._reader.join(timeout=3)
        if self.proc:
            for stream in (self.proc.stdin, self.proc.stdout):
                if stream:
                    stream.close()
        log(f"starting {self.exe.name}")
        env_kw = {}
        if sys.platform != "win32":
            env_kw["env"] = {**os.environ, "LD_LIBRARY_PATH": str(self.root)}
        self.proc = subprocess.Popen(
            self.command or [str(self.exe)],
            cwd=str(self.root),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8', errors='replace',
            bufsize=1,
            **env_kw,
        )
        self._reader = threading.Thread(target=self._pump_output, daemon=True)
        self._reader.start()

    def _pump_output(self) -> None:
        assert self.proc and self.proc.stdout
        handler = None
        logger = logging.getLogger(f'bedrock-server-{id(self)}')
        logger.setLevel(logging.INFO)
        logger.propagate = False
        try:
            if self.log_path:
                handler = RotatingFileHandler(self.log_path, maxBytes=5*1024**2, backupCount=5, encoding='utf-8')
                handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
                logger.addHandler(handler)
            for line in self.proc.stdout:
                line = line.rstrip("\n")
                if self.on_line:
                    try:
                        self.on_line(line)
                    except Exception as exc:
                        log(f'WARNING: could not record server state: {exc}')
                print(line, flush=True)
                hit = MGR_MARKER.search(line)
                if hit and self.on_command:
                    try:
                        self.on_command(hit.group(1), hit.group(2).strip())
                    except Exception as exc:  # never kill the reader thread
                        log(f"in-game command failed: {exc}")
                if handler:
                    logger.info(line)
        except (ValueError, OSError):
            pass
        finally:
            if handler:
                logger.removeHandler(handler)
                handler.close()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def send(self, command: str) -> bool:
        """Write a command to the server console."""
        with self._lock:
            if not self.alive() or not self.proc or not self.proc.stdin:
                return False
            try:
                self.proc.stdin.write(command.rstrip("\n") + "\n")
                self.proc.stdin.flush()
                return True
            except (OSError, ValueError):
                return False

    def stop(self, timeout: int = STOP_TIMEOUT) -> None:
        if not self.alive():
            return
        log("sending 'stop' for a clean shutdown (saves the world)")
        if not self.send("stop"):
            log("could not write to stdin; terminating")
            self.proc.terminate()
        assert self.proc
        try:
            self.proc.wait(timeout=timeout)
            log("server exited cleanly")
        except subprocess.TimeoutExpired:
            log(f"still running after {timeout}s - forcing it down")
            self.proc.kill()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                log("WARNING: process would not die")

    # -- player-facing messages -----------------------------------------

    def push_info(self, schedule: str, next_time: str, next_in: str) -> None:
        """Send current state to the companion pack so its GUI can show it."""
        self.send(f"scriptevent {NS_BACK}:info {schedule}|{next_time}|{next_in}")

    def announce(self, text: str, color: str = COLOR_WARN) -> None:
        payload = json.dumps({'rawtext': [{'text': color + text}]}, ensure_ascii=False)
        self.send(f"tellraw @a {payload}")

    def actionbar(self, text: str, color: str = COLOR_WARN) -> None:
        payload = json.dumps({'rawtext': [{'text': color + text}]}, ensure_ascii=False)
        self.send(f"titleraw @a actionbar {payload}")


# ---------------------------------------------------------------------------


class Manager:
    def __init__(self, root: Path, args):
        self.root = root
        self.args = args
        self.config_path = root / CONFIG_FILE
        self.config = load_config(self.config_path)
        self.times = parse_times(self.config["restart_times"])
        self.warn_minutes = self.config["warn_minutes"]
        self.stopping = threading.Event()
        self.restart_now = threading.Event()
        self.schedule_changed = threading.Event()
        self.cancelled_flag = threading.Event()
        self.manual_active = threading.Event()
        self.skip_target: datetime | None = None
        self.current_target: datetime | None = None
        self.commands = queue.Queue()
        self.state_lock = threading.RLock()
        self.players = set()
        self.server_up = False
        self.started_at = None
        self.version = ''
        self.maintenance = ''
        self._queue_due = {}
        self.history = None
        self.runtime = None
        self._scheduled_job = None
        self._job_checked = 0
        log_path = (root / LOG_FILE) if LOG_FILE and not getattr(args, 'daemon', False) else None
        self.server = ServerProcess(root, log_path, on_command=self.handle_ingame,
                                    on_line=self.on_output, command=getattr(args, 'server_command', None))

    def snapshot(self):
        with self.state_lock:
            return {'players': sorted(self.players), 'server_up': self.server_up,
                    'player_queue_protocol': 2,
                    'started_at': self.started_at, 'version': self.version,
                    'next_restart': self.current_target.isoformat() if self.current_target else None,
                    'maintenance': self.maintenance, 'stopping': self.stopping.is_set(),
                    'pid': self.server.proc.pid if self.server.proc else None}

    def on_output(self, line):
        from player_history import RE_CONNECT, RE_DISCONNECT
        if self.history:
            self.history.ingest_line(line)
        joined, left = RE_CONNECT.search(line), RE_DISCONNECT.search(line)
        with self.state_lock:
            if joined:
                name = joined.group(1).strip()
                if name not in self.players:
                    self._queue_due[name] = (time.monotonic() + 6,
                                             self.history.queue_watermark() if self.history else 0)
                self.players.add(name)
            if left:
                name = left.group(1).strip()
                self.players.discard(name)
                self._queue_due.pop(name, None)
            version = re.search(r'Version:\s*([\d.]+)', line)
            if version:
                self.version = version[1]
            if 'Server started.' in line:
                self.server_up = True
                self.started_at = now().isoformat()
            if 'Stopping server' in line or 'Quit correctly' in line:
                self.server_up = False
                self.players.clear()
                self._queue_due.clear()
                if self.history:
                    self.history.end_sessions()

    def reset_live_state(self):
        """An abrupt exit has no disconnect lines; close those sessions explicitly."""
        if self.server._reader:
            self.server._reader.join(timeout=3)
        with self.state_lock:
            self.server_up = False
            self.started_at = None
            self.players.clear()
            self._queue_due.clear()
        if self.history:
            self.history.end_sessions()

    def scheduled_update(self):
        if not getattr(getattr(self, 'args', None), 'daemon', False):
            return None
        if time.monotonic() - self._job_checked < 5:
            return self._scheduled_job
        self._job_checked = time.monotonic()
        self._scheduled_job = None
        try:
            ui = json.loads((self.root / 'launcher_ui.json').read_text(encoding='utf-8-sig'))
            job = ui.get('update_scheduled')
            if not isinstance(job, dict):
                return None
            from bedrock_update import valid_version
            if not valid_version(job.get('version', '')):
                return None
            due = datetime.strptime(job['at'], '%Y-%m-%d %H:%M')
            history = json.loads((self.root / 'maintenance_jobs.json').read_text()) if (self.root / 'maintenance_jobs.json').exists() else {}
            key = job['version'] + '@' + job['at']
            if key in history or now() < due:
                return None
            if (now()-due).total_seconds() > 7200:
                history[key] = {'state': 'missed', 'detail': 'More than two hours past the scheduled time.'}
                atomic_json(self.root / 'maintenance_jobs.json', history)
                log('WARNING: missed scheduled update; it was not run late.')
                return None
            self._scheduled_job = {**job, 'key': key}
            return self._scheduled_job
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def run_scheduled_update(self, job):
        from bedrock_update import download, _apply_update_locked
        jobs_path = self.root / 'maintenance_jobs.json'
        history = json.loads(jobs_path.read_text()) if jobs_path.exists() else {}
        self.maintenance = 'Preparing scheduled update'
        try:
            archive = download(job['version'], self.root)
            if not self.wait_for_players('Server update'):
                return
            self.server.announce('[Server] Updating in one minute. The world will be backed up.', COLOR_URGENT)
            if self.stopping.wait(60):
                return
            self.maintenance = 'Installing scheduled update'
            self.server.stop()
            if self.server.alive():
                raise RuntimeError('Server could not be stopped.')
            report = _apply_update_locked(self.root, archive, job['version'])
            history[job['key']] = {'state': 'completed', 'detail': f"Installed {report['to']}", 'at': now().isoformat()}
            log(f"scheduled update installed {report['to']}")
            if job.get('restart', True):
                self.server.start()
            else:
                self.stopping.set()
        except Exception as exc:
            history[job['key']] = {'state': 'failed', 'detail': str(exc), 'at': now().isoformat()}
            log(f'ERROR: scheduled update failed: {exc}')
            # Stop for inspection if an update failed after shutdown; do not boot mixed files.
            if not self.server.alive():
                self.stopping.set()
        finally:
            atomic_json(jobs_path, history)
            self._scheduled_job = None
            self.maintenance = ''

    def service_history(self):
        while not self.stopping.wait(1):
            if not self.history:
                continue
            with self.state_lock:
                due = [name for name, (when, _limit) in self._queue_due.items() if when <= time.monotonic()]
            for name in due:
                with self.state_lock:
                    if self.maintenance:
                        continue
                    _when, watermark = self._queue_due.pop(name, (0, 0))
                    if name not in self.players:
                        continue
                try:
                    sent = self.history.queue_deliver(name, self.server.send, before_id=watermark)
                    for item in sent:
                        log(f"queued command sent for {name}: {item['command']}")
                except Exception as exc:
                    log(f'WARNING: queued command delivery failed: {exc}')

    def wait_for_players(self, label):
        self.config = load_config(self.config_path)
        if not self.config.get('maintenance_wait_empty', False):
            return True
        try:
            maximum = max(0, min(1440, int(self.config.get('maintenance_max_delay', 30))))
        except (ValueError, TypeError):
            maximum = 30
        deadline = time.monotonic() + maximum * 60
        if self.players:
            self.server.announce(f'[Server] {label} is waiting for players to leave (up to {maximum} minutes).')
        self.maintenance = f'Waiting for players: {label}'
        while self.players and time.monotonic() < deadline:
            if self.stopping.wait(1):
                return False
        self.maintenance = ''
        if self.players:
            self.server.announce(f'[Server] {label} will begin in one minute; the maintenance window has arrived.', COLOR_URGENT)
            if self.stopping.wait(60):
                return False
        return not self.stopping.is_set()

    # -- commands arriving from in-game -----------------------------------

    def handle_ingame(self, command: str, payload: str) -> None:
        log(f"in-game command: {command} {payload!r}")

        if command == "restart":
            self.cancelled_flag.clear()
            self.manual_active.set()
            self.restart_now.set()
            self.schedule_changed.set()

        elif command == "cancel":
            if self.manual_active.is_set():
                # a manually triggered restart is counting down - stop it
                self.restart_now.clear()
                self.cancelled_flag.set()
                self.manual_active.clear()
                self.server.announce("[Server] Restart cancelled.", COLOR_OK)
            else:
                self.skip_target = self.current_target
                self.schedule_changed.set()
                self.server.announce("[Server] Next scheduled restart skipped.", COLOR_OK)
                self.sync_info()

        elif command == "next":
            # compute live - current_target can be stale for a moment after
            # a schedule change, before the main loop picks it up
            nxt = self.pick_target()
            self.server.announce(
                f"[Server] Next restart {nxt:%H:%M} (in {human(nxt - now())}).", COLOR_OK)
            times = ", ".join(f"{h:02d}:{m:02d}" for h, m in self.times)
            self.server.announce(f"[Server] Schedule: {times}", COLOR_OK)

        elif command in {"sync", "ready"}:
            self.sync_info()

        elif command in {"menu", "ev"}:
            # menu is handled in-game; ev is activity data for the launcher,
            # which reads it off our stdout. Neither needs a reply.
            pass

        elif command == "schedule":
            self.change_schedule(payload)

        elif command == "help":
            for line in (
                "[Server] Restart manager commands (operators):",
                "  /scriptevent mgr:restart    - restart with a 1 minute warning",
                "  /scriptevent mgr:cancel     - cancel or skip the next restart",
                "  /scriptevent mgr:next       - when is the next restart",
                "  /scriptevent mgr:schedule 06:00,14:00,22:00",
            ):
                self.server.announce(line, COLOR_OK)

        else:
            self.server.announce(f"[Server] Unknown restart command: {command}", COLOR_URGENT)

    def sync_info(self) -> None:
        """Tell the in-game pack the current schedule and next restart."""
        nxt = self.pick_target()
        schedule = ",".join(f"{h:02d}:{m:02d}" for h, m in self.times)
        self.server.push_info(schedule, f"{nxt:%H:%M}", human(nxt - now()))

    def change_schedule(self, payload: str) -> None:
        raw = [p for p in re.split(r"[,\s]+", payload.strip()) if p]
        if not raw:
            self.server.announce(
                "[Server] Usage: /scriptevent mgr:schedule 06:00,14:00,22:00", COLOR_URGENT)
            return
        try:
            times = parse_times(raw)
        except SystemExit as exc:
            self.server.announce(f"[Server] {exc}", COLOR_URGENT)
            return

        self.times = times
        # Re-read from disk before saving. Something else (the launcher, or a
        # hand edit) may have changed warn_minutes since we started, and our
        # in-memory copy would otherwise clobber it.
        disk = load_config(self.config_path)
        disk["restart_times"] = [f"{h:02d}:{m:02d}" for h, m in times]
        self.config = disk
        if disk.get("warn_minutes") and disk["warn_minutes"] != self.warn_minutes:
            self.warn_minutes = disk["warn_minutes"]
            log("warning times updated to " + ", ".join(f"{m}m" for m in self.warn_minutes))
        try:
            save_config(self.config_path, self.config)
        except OSError as exc:
            log(f"WARNING: could not save {CONFIG_FILE}: {exc}")
            self.server.announce("[Server] Schedule changed, but saving to disk failed.",
                                 COLOR_URGENT)
        self.skip_target = None
        self.schedule_changed.set()
        pretty = ", ".join(self.config["restart_times"])
        log(f"schedule changed to {pretty}")
        self.server.announce(f"[Server] Restart schedule is now {pretty}.", COLOR_OK)
        self.sync_info()

    # -- the restart countdown -------------------------------------------

    def countdown(self, target: datetime) -> None:
        """Warn players as `target` approaches. Returns when it's time."""
        warned_min: set[int] = set()
        warned_sec: set[int] = set()

        while not self.stopping.is_set():
            remaining = (target - now()).total_seconds()
            if remaining <= 0:
                return
            if self.scheduled_update():
                return
            if not self.server.alive():
                return
            if self.restart_now.is_set() or self.schedule_changed.is_set():
                return

            mins_left = int(remaining // 60)
            secs_left = int(remaining)

            for mark in self.warn_minutes:
                if mark not in warned_min and mins_left < mark and remaining > mark * 60 - 60:
                    warned_min.add(mark)
                    colour = COLOR_URGENT if mark <= 2 else COLOR_WARN
                    self.server.announce(
                        f"[Server] Restarting in {plural(mark, 'minute')}. "
                        "Finish what you're doing and find a safe spot.", colour)
                    self.server.actionbar(f"Restart in {plural(mark, 'minute')}", colour)
                    log(f"warned players: {mark} min")

            if remaining <= max(WARN_SECONDS) + 1:
                for mark in WARN_SECONDS:
                    if mark not in warned_sec and mark - 1 <= remaining <= mark:
                        warned_sec.add(mark)
                        self.server.actionbar(f"Restart in {mark}...", COLOR_URGENT)

            # sleep in small slices so Ctrl+C stays responsive
            time.sleep(0.5 if remaining <= max(WARN_SECONDS) + 2 else 2)

    def do_restart(self) -> None:
        self.server.announce("[Server] Restarting now - back in about a minute.", COLOR_URGENT)
        self.server.actionbar("Restarting now", COLOR_URGENT)
        time.sleep(2)
        self.server.stop()
        if self.server.alive():
            raise RuntimeError('Server did not stop; refusing to back up or start a second process.')
        if self.stopping.is_set():
            return
        self.config = load_config(self.config_path)
        if self.config.get("backup_before_restart"):
            self.maintenance = 'Backing up before restart'
            self.backup_world()
            self.maintenance = ''
        log(f"waiting {RESTART_DELAY}s before relaunch")
        if self.stopping.wait(RESTART_DELAY):
            return
        self.server.start()
        time.sleep(3)
        self.server.announce("[Server] Back up. Have fun.", COLOR_OK)
        self.sync_info()

    def level_name(self) -> str:
        """Read level-name straight from server.properties."""
        try:
            for line in (self.root / "server.properties").read_text(
                    encoding="utf-8-sig", errors="replace").splitlines():
                st = line.strip()
                if st.startswith("#") or "=" not in st:
                    continue
                k, _, v = st.partition("=")
                if k.strip() == "level-name":
                    return v.strip()
        except OSError:
            pass
        return "Bedrock level"

    def backup_world(self) -> None:
        """Zip the world while the server is stopped - the only safe moment."""
        try:
            log("backing up the world before restarting...")
            start = time.monotonic()
            path = create_backup(self.root, 'auto')
            log(f"backup written: backups/{path.name} "
                f"({path.stat().st_size/1048576:.0f} MB in {time.monotonic()-start:.0f}s)")
            self.prune_backups(path.parent, self.config.get('backup_keep', 10))
        except Exception as exc:
            log(f"WARNING: backup failed ({exc}) - continuing with the restart")

    def prune_backups(self, dest: Path, keep: int = 10) -> None:
        try:
            for old in prune_automatic(self.root, keep):
                log(f"removed old backup {old.name}")
        except OSError:
            pass

    # -- input forwarding -------------------------------------------------

    def console_loop(self) -> None:
        """Forward what you type to the server console."""
        while not self.stopping.is_set():
            try:
                if getattr(self.args, 'daemon', False):
                    try:
                        line = self.commands.get(timeout=1)
                    except queue.Empty:
                        continue
                else:
                    line = input()
            except (EOFError, KeyboardInterrupt):
                self.stopping.set()
                return
            if not line.strip():
                continue
            low = line.strip().lower()
            if low in {"!restart", "!r"}:
                log("manual restart requested")
                self.cancelled_flag.clear()
                self.manual_active.set()
                self.restart_now.set()
                continue
            if low.startswith("!schedule"):
                self.change_schedule(line.strip()[len("!schedule"):])
                continue
            if low in {"!skip"}:
                self.skip_target = self.current_target
                self.schedule_changed.set()
                self.server.announce("[Server] Next scheduled restart skipped.", COLOR_OK)
                log("next scheduled restart will be skipped")
                continue
            if low in {"!sync"}:
                self.sync_info()
                log("pushed schedule info to the in-game pack")
                continue
            if low in {"!next", "!n"}:
                nxt = next_restart(self.times, now())
                log(f"next restart {nxt:%H:%M} (in {human(nxt - now())})")
                continue
            if low in {"!quit", "!exit"}:
                self.stopping.set()
                return
            self.server.send(line)

    def _safe_sync(self) -> None:
        if not self.stopping.is_set() and self.server.alive():
            try:
                self.sync_info()
            except Exception as exc:
                log(f"sync failed: {exc}")

    def pick_target(self) -> datetime:
        """Next scheduled restart, skipping one if it was cancelled in-game."""
        target = next_restart(self.times, now())
        if self.skip_target and abs((target - self.skip_target).total_seconds()) < 60:
            target = next_restart(self.times, target)
        return target

    def restart_pending(self) -> bool:
        """False once someone cancels during the one-minute manual warning."""
        return not self.cancelled_flag.is_set()

    # -- main loop --------------------------------------------------------

    def run(self) -> int:
        from player_history import PlayerHistory
        self.history = PlayerHistory(self.root)
        self.history.close_open_sessions()
        history_worker = threading.Thread(target=self.service_history, daemon=True)
        history_worker.start()
        self._sync_timer = None
        try:
            return self._run_loop()
        finally:
            self.stopping.set()
            if self._sync_timer:
                self._sync_timer.cancel()
            self.server.stop()
            history_worker.join(timeout=5)
            self.reset_live_state()
            self.history.close()

    def _run_loop(self) -> int:
        log(f"server directory: {self.root}")
        log("restart schedule: " + ", ".join(f"{h:02d}:{m:02d}" for h, m in self.times))
        log(f"schedule file: {self.config_path}")
        log("warnings at: " + ", ".join(f"{m}m" for m in self.warn_minutes))
        log("commands: !restart  !skip  !next  !sync  !schedule HH:MM,HH:MM  !quit")

        self.server.start()

        # give the world a few seconds to load, then prime the pack's GUI
        timer = threading.Timer(20.0, self._safe_sync)
        self._sync_timer = timer
        timer.daemon = True
        timer.start()

        console = threading.Thread(target=self.console_loop, daemon=True)
        console.start()

        if self.args.now:
            log("--now given: restarting in 60 seconds as a test")
            target = now() + timedelta(seconds=60)
        else:
            target = self.pick_target()
        self.current_target = target
        log(f"next restart at {target:%Y-%m-%d %H:%M} (in {human(target - now())})")

        crashes = []
        failed = False
        try:
            while not self.stopping.is_set():
                job = self.scheduled_update()
                if job:
                    self.run_scheduled_update(job)
                    target = self.pick_target()
                    self.current_target = target
                    continue
                if not self.server.alive():
                    self.reset_live_state()
                    current = time.monotonic()
                    crashes = [t for t in crashes if current - t < 600] + [current]
                    if len(crashes) >= self.config.get('crash_restart_limit', 5):
                        log('ERROR: repeated crashes within 10 minutes; automatic recovery paused. Check the last server errors.')
                        self.stopping.set()
                        failed = True
                        break
                    delay = min(60, RESTART_DELAY * 2 ** (len(crashes) - 1))
                    log(f"server exited on its own - restarting in {delay}s (attempt {len(crashes)})")
                    if self.stopping.wait(delay):
                        break
                    self.server.start()
                    time.sleep(3)
                    self.server.announce("[Server] Recovered after an unexpected stop.", COLOR_OK)

                if self.schedule_changed.is_set() and not self.restart_now.is_set():
                    self.schedule_changed.clear()
                    target = self.pick_target()
                    self.current_target = target
                    log(f"next restart at {target:%Y-%m-%d %H:%M} (in {human(target - now())})")
                    continue

                if now() >= target or self.restart_now.is_set():
                    manual = self.restart_now.is_set()
                    if self.restart_now.is_set():
                        self.restart_now.clear()
                        self.schedule_changed.clear()
                        self.server.announce("[Server] Restarting in 1 minute.", COLOR_URGENT)
                        for _ in range(60):
                            if self.stopping.is_set() or not self.restart_pending():
                                break
                            time.sleep(1)
                        cancelled = not self.restart_pending()
                        self.manual_active.clear()
                        if cancelled:
                            log("restart cancelled from in-game")
                            target = self.pick_target()
                            self.current_target = target
                            continue
                    if (manual or self.wait_for_players('Scheduled restart')) and not self.stopping.is_set():
                        self.do_restart()
                    self.skip_target = None
                    target = self.pick_target()
                    self.current_target = target
                    log(f"next restart at {target:%Y-%m-%d %H:%M} (in {human(target - now())})")
                    continue

                self.countdown(target)
        except KeyboardInterrupt:
            log("Ctrl+C - shutting down")
            self.stopping.set()
            self.server.announce("[Server] Shutting down.", COLOR_URGENT)
            time.sleep(1)
        except Exception:
            self.stopping.set()
            self.server.stop()
            raise

        self.stopping.set()
        self.server.stop()
        log("manager exiting")
        return 1 if failed else 0


# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="Supervise a Bedrock server with scheduled restarts.")
    ap.add_argument("--server", type=Path, default=Path(__file__).resolve().parent,
                    help="bedrock-server directory (default: this script's folder)")
    ap.add_argument("--now", action="store_true",
                    help="Restart 60 seconds from launch, to test the warnings")
    ap.add_argument("--check", action="store_true",
                    help="Print the schedule and exit without starting anything")
    ap.add_argument('--daemon', action='store_true', help='Run independently with local authenticated launcher control')
    args = ap.parse_args()

    if args.check:
        cfg = load_config(args.server.resolve() / CONFIG_FILE)
        times = parse_times(cfg["restart_times"])
        print("Restart schedule (local time):")
        cursor = now()
        for _ in range(len(times) + 1):
            cursor = next_restart(times, cursor)
            print(f"  {cursor:%a %Y-%m-%d %H:%M}   (in {human(cursor - now())})")
        print("\nWarnings at: " + ", ".join(f"{m} min" for m in cfg["warn_minutes"]))
        print("Final countdown at: " + ", ".join(f"{s}s" for s in WARN_SECONDS))
        return 0

    try:
        with operation_lock(args.server.resolve()):
            from bedrock_recovery import assert_recovered
            assert_recovered(args.server.resolve())
            from bedrock_update import server_running
            if server_running():
                raise RuntimeError('A Bedrock server is already running. Stop it before starting another manager.')
            manager = Manager(args.server.resolve(), args)
            if args.daemon:
                from bedrock_runtime import serve
                return serve(manager)
            return manager.run()
    except (OSError, RuntimeError) as exc:
        log(f'ERROR: {exc}')
        return 1


if __name__ == "__main__":
    sys.exit(main())
