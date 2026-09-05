#!/usr/bin/env python3
"""
player_history.py - persistent per-player activity log and command queue
for a Minecraft Bedrock server.

Used by BedrockLauncher.pyw, but standalone and importable:

    from player_history import PlayerHistory
    h = PlayerHistory(Path("."))
    h.ingest_line("[2026-08-27 17:28:11:553 INFO] Player connected: Steve, xuid: 123")
    h.timeline("Steve")

Storage is a single SQLite file (players.db) next to the server executable.
SQLite ships with Python, so there is nothing to install.

Three sources feed it:
  1. Console lines the server prints (connects and disconnects).
  2. [MGR]|ev|{json} lines emitted by the Restart Manager Link behavior pack,
     which sees deaths and chat that the console never prints.
  3. Anything else on the console that mentions a known player's name.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

DB_NAME = "players.db"

# BDS console shapes
RE_CONNECT = re.compile(r"Player connected:\s*(.+?),\s*xuid:\s*(\S+)")
RE_DISCONNECT = re.compile(r"Player disconnected:\s*(.+?),\s*xuid:\s*(\S+)")
RE_SPAWNED = re.compile(r"Player Spawned:\s*(.+?)\s+xuid:\s*(\S+)")
RE_TIMESTAMP = re.compile(r"\[(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})")
RE_EVENT = re.compile(r"\[MGR\]\|ev\|(.*)$")
RE_MGR_ANY = re.compile(r"\[MGR\]\||^\[manager ")
RE_COLOR = re.compile(r"§.")

KINDS = ["join", "leave", "death", "chat", "command", "queue", "console"]

KIND_LABEL = {
    "join": "joined",
    "leave": "left",
    "death": "died",
    "chat": "chat",
    "command": "command",
    "queue": "queued",
    "console": "console",
}


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def line_time(line: str) -> str:
    """Prefer the server's own timestamp when the line carries one."""
    m = RE_TIMESTAMP.search(line)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    return now_iso()


def strip_codes(text: str) -> str:
    return RE_COLOR.sub("", text).strip()


class PlayerHistory:
    def __init__(self, root: Path):
        self.path = Path(root) / DB_NAME
        self._lock = threading.RLock()
        self._open()
        try:
            self._migrate()
        except Exception:
            self.db.close()
            raise
        self._names: set[str] = set()
        self._name_patterns = {}
        self._reload_names()

    # -- schema -----------------------------------------------------------

    def _open(self) -> None:
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row

    def _quarantine(self, why: str) -> None:
        """
        Move a damaged database aside and start a new one. Losing history is
        bad; refusing to run at all is worse.
        """
        try:
            self.db.close()
        except Exception:
            pass
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        bad = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        try:
            self.path.rename(bad)
            print(f"player_history: {why}; moved the damaged file to {bad.name}")
        except OSError as exc:
            raise RuntimeError('Could not preserve the damaged history database; recovery stopped.') from exc
        for suffix in ("-wal", "-shm", "-journal"):
            side = self.path.with_name(self.path.name + suffix)
            try:
                if side.exists():
                    side.rename(bad.with_name(bad.name + suffix))
            except OSError as exc:
                raise RuntimeError(f'Could not preserve database recovery file {side.name}.') from exc
        self._open()

    def _migrate(self) -> None:
        try:
            self._create_schema()
        except sqlite3.DatabaseError as exc:
            # Locks, read-only files and disk-full errors are not corruption.
            code = getattr(exc, 'sqlite_errorcode', 0) & 0xff
            corrupt = code in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB)
            if not code:  # Python 3.10 does not expose sqlite_errorcode.
                corrupt = str(exc) in ('file is not a database', 'database disk image is malformed')
            if not corrupt:
                raise
            self._quarantine(str(exc))
            self._create_schema()

    def _create_schema(self) -> None:
        with self._lock:
            # WAL is faster but needs shared memory, which network shares and
            # some mounted filesystems refuse. Fall back rather than die.
            for mode in ("WAL", "TRUNCATE", "DELETE"):
                try:
                    self.db.execute(f"PRAGMA journal_mode={mode}")
                    break
                except sqlite3.OperationalError:
                    continue
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS players (
                name          TEXT PRIMARY KEY,
                xuid          TEXT,
                first_seen    TEXT,
                last_seen     TEXT,
                sessions      INTEGER DEFAULT 0,
                seconds       INTEGER DEFAULT 0,
                online        INTEGER DEFAULT 0,
                session_start TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                ts     TEXT NOT NULL,
                player TEXT NOT NULL,
                kind   TEXT NOT NULL,
                detail TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ev_player ON events(player, id DESC);
            CREATE INDEX IF NOT EXISTS idx_ev_ts ON events(ts);
            CREATE TABLE IF NOT EXISTS queue (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                player  TEXT NOT NULL,
                command TEXT NOT NULL,
                created TEXT,
                fired   TEXT,
                status  TEXT DEFAULT 'pending'
            );
            CREATE INDEX IF NOT EXISTS idx_q_player ON queue(player, status);
            """)
            columns = {row[1] for row in self.db.execute('PRAGMA table_info(queue)')}
            if 'xuid' not in columns:
                self.db.execute("ALTER TABLE queue ADD COLUMN xuid TEXT DEFAULT ''")
            self.db.commit()

    def _reload_names(self) -> None:
        with self._lock:
            rows = self.db.execute("SELECT name FROM players").fetchall()
        self._names = {r["name"] for r in rows}
        self._name_patterns = {name: re.compile(rf'(?<![\w]){re.escape(name)}(?![\w])')
                               for name in self._names if len(name) >= 3}

    # -- writing ----------------------------------------------------------

    def record(self, player: str, kind: str, detail: str = "",
               ts: str | None = None) -> dict:
        ts = ts or now_iso()
        with self._lock:
            self.db.execute(
                "INSERT INTO events (ts, player, kind, detail) VALUES (?,?,?,?)",
                (ts, player, kind, detail[:500]))
            if kind not in ('command', 'queue'):
                self.db.execute(
                    "UPDATE players SET last_seen=? WHERE name=? AND "
                    "(last_seen IS NULL OR last_seen < ?)", (ts, player, ts))
            self.db.commit()
        return {"ts": ts, "player": player, "kind": kind, "detail": detail}

    def _ensure_player(self, name: str, xuid: str = "", ts: str = "") -> None:
        ts = ts or now_iso()
        with self._lock:
            self.db.execute(
                "INSERT INTO players (name, xuid, first_seen, last_seen) "
                "VALUES (?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                "xuid=COALESCE(NULLIF(excluded.xuid,''), players.xuid), "
                "last_seen=MAX(players.last_seen, excluded.last_seen)",
                (name, xuid, ts, ts))
            self.db.commit()
        self._names.add(name)
        if len(name) >= 3 and name not in self._name_patterns:
            self._name_patterns[name] = re.compile(rf'(?<![\w]){re.escape(name)}(?![\w])')

    def player_joined(self, name: str, xuid: str = "", ts: str = "") -> dict | None:
        ts = ts or now_iso()
        self._ensure_player(name, xuid, ts)
        with self._lock:
            row = self.db.execute('SELECT online FROM players WHERE name=?', (name,)).fetchone()
            if row and row['online']:
                return None
            self.db.execute(
                "UPDATE players SET online=1, session_start=?, sessions=sessions+1 "
                "WHERE name=?", (ts, name))
            self.db.commit()
        return self.record(name, "join", "connected", ts)

    def player_left(self, name: str, ts: str = "") -> dict | None:
        ts = ts or now_iso()
        self._ensure_player(name, "", ts)
        seconds = 0
        with self._lock:
            row = self.db.execute(
                "SELECT online, session_start FROM players WHERE name=?", (name,)).fetchone()
            if row and not row['online']:
                return None
            if row and row["session_start"]:
                try:
                    start = datetime.fromisoformat(row["session_start"])
                    seconds = max(0, int((datetime.fromisoformat(ts) - start)
                                         .total_seconds()))
                except ValueError:
                    seconds = 0
            self.db.execute(
                "UPDATE players SET online=0, session_start=NULL, "
                "seconds=seconds+? WHERE name=?", (seconds, name))
            self.db.commit()
        detail = f"session {fmt_span(seconds)}" if seconds else "disconnected"
        return self.record(name, "leave", detail, ts)

    def close_open_sessions(self) -> int:
        """Called at launcher start - anyone still flagged online is stale."""
        with self._lock:
            rows = self.db.execute(
                "SELECT name FROM players WHERE online=1").fetchall()
            self.db.execute(
                "UPDATE players SET online=0, session_start=NULL WHERE online=1")
            self.db.commit()
        return len(rows)

    def end_sessions(self, ts: str | None = None) -> None:
        """Close live sessions at server shutdown, retaining elapsed playtime."""
        with self._lock:
            names = [r['name'] for r in self.db.execute('SELECT name FROM players WHERE online=1')]
        for name in names:
            self.player_left(name, ts or now_iso())

    # -- ingesting console output -----------------------------------------

    def ingest_line(self, line: str) -> list[dict]:
        """Feed one console line. Returns the events it produced."""
        plain = strip_codes(line)
        ts = line_time(plain)
        out: list[dict] = []

        m = RE_EVENT.search(plain)
        if m:
            return self._ingest_event(m.group(1), ts)

        m = RE_CONNECT.search(plain)
        if m:
            event = self.player_joined(m.group(1).strip(), m.group(2).strip(), ts)
            return [event] if event else []

        m = RE_DISCONNECT.search(plain)
        if m:
            event = self.player_left(m.group(1).strip(), ts)
            return [event] if event else []

        m = RE_SPAWNED.search(plain)
        if m:
            self._ensure_player(m.group(1).strip(), m.group(2).strip(), ts)
            return []

        # catch-all: any other console line naming a known player
        if RE_MGR_ANY.search(plain) or not self._names:
            return out
        body = re.sub(r"^\[[^\]]*\]\s*", "", plain).strip()
        if not body:
            return out
        for name, pattern in tuple(self._name_patterns.items()):
            if pattern.search(body):
                out.append(self.record(name, "console", body, ts))
        return out

    def _ingest_event(self, payload: str, ts: str) -> list[dict]:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, dict):
            return []
        name = str(data.get("p") or "").strip()
        kind = str(data.get("k") or "").strip()
        detail = strip_codes(str(data.get("d") or ""))
        if not name or kind not in KINDS:
            return []
        if kind == "join":
            event = self.player_joined(name, str(data.get("x") or ""), ts)
            return [event] if event else []
        if kind == "leave":
            event = self.player_left(name, ts)
            return [event] if event else []
        self._ensure_player(name, "", ts)
        return [self.record(name, kind, detail, ts)]

    # -- reading -----------------------------------------------------------

    def players(self) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM players ORDER BY online DESC, last_seen DESC"
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            live = 0
            if d["online"] and d["session_start"]:
                try:
                    live = int((datetime.now() -
                                datetime.fromisoformat(d["session_start"]))
                               .total_seconds())
                except ValueError:
                    live = 0
            d["playtime"] = fmt_span((d["seconds"] or 0) + live)
            out.append(d)
        return out

    def timeline(self, player: str | None = None, kinds: list[str] | None = None,
                 search: str = "", since_hours: int | None = None,
                 limit: int = 500) -> list[dict]:
        sql = "SELECT * FROM events WHERE 1=1"
        args: list = []
        if player:
            sql += " AND player=?"
            args.append(player)
        if kinds:
            sql += " AND kind IN (%s)" % ",".join("?" * len(kinds))
            args += kinds
        if search:
            sql += " AND (detail LIKE ? OR player LIKE ?)"
            args += [f"%{search}%", f"%{search}%"]
        if since_hours:
            cutoff = (datetime.now() - timedelta(hours=since_hours)) \
                .replace(microsecond=0).isoformat(sep=" ")
            sql += " AND ts >= ?"
            args.append(cutoff)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(10000, int(limit))))
        with self._lock:
            rows = self.db.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        with self._lock:
            p = self.db.execute("SELECT COUNT(*) c FROM players").fetchone()["c"]
            e = self.db.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
            q = self.db.execute(
                "SELECT COUNT(*) c FROM queue WHERE status='pending'").fetchone()["c"]
        return {"players": p, "events": e, "queued": q}

    # -- the command queue --------------------------------------------------

    def queue_add(self, player: str, command: str) -> int:
        player = player.strip()
        command = normalize_queue_command(command)
        if not player or any(ord(c) < 32 for c in player):
            raise ValueError('Choose a player and one single-line command.')
        with self._lock:
            row = self.db.execute('SELECT xuid FROM players WHERE name=?', (player,)).fetchone()
            cur = self.db.execute(
                "INSERT INTO queue (player, command, created, xuid) VALUES (?,?,?,?)",
                (player, command, now_iso(), (row['xuid'] or '') if row else ''))
            self.db.commit()
            return cur.lastrowid

    def queue_counts(self) -> dict:
        with self._lock:
            return {r['player']: r['count'] for r in self.db.execute(
                "SELECT player, COUNT(*) count FROM queue WHERE status='pending' GROUP BY player")}

    def queue_watermark(self) -> int:
        with self._lock:
            return self.db.execute('SELECT COALESCE(MAX(id), 0) FROM queue').fetchone()[0]

    def queue_for(self, player: str | None = None,
                  include_done: bool = False) -> list[dict]:
        sql = "SELECT * FROM queue WHERE 1=1"
        args: list = []
        if player:
            sql += " AND player=?"
            args.append(player)
        if not include_done:
            sql += " AND status='pending'"
        sql += " ORDER BY id"
        with self._lock:
            rows = self.db.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def queue_take(self, player: str) -> list[dict]:
        """Pending commands for a player, marked fired as they are handed over."""
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM queue WHERE player=? AND status='pending' ORDER BY id",
                (player,)).fetchall()
            if rows:
                self.db.execute(
                    "UPDATE queue SET status='fired', fired=? "
                    "WHERE player=? AND status='pending'", (now_iso(), player))
                self.db.commit()
        return [dict(r) for r in rows]

    def queue_delete(self, queue_id: int, player: str | None = None) -> bool:
        with self._lock:
            sql = "DELETE FROM queue WHERE id=?"
            args = [queue_id]
            if player is not None:
                sql += " AND player=? AND status='pending'"
                args.append(player)
            cur = self.db.execute(sql, args)
            self.db.commit()
            return bool(cur.rowcount)

    def queue_deliver(self, player: str, send, before_id: int | None = None) -> list[dict]:
        """Mark sent only after stdin accepts a command; failures stay pending.

        This records pipe delivery, not execution confirmation from Bedrock.
        """
        delivered = []
        with self._lock:
            # Recheck inside a write transaction: two launcher/manager connections
            # must never dispatch the same pending item concurrently.
            for candidate in self.queue_for(player):
                if before_id is not None and candidate['id'] > before_id:
                    continue
                self.db.execute('BEGIN IMMEDIATE')
                try:
                    item = self.db.execute("SELECT * FROM queue WHERE id=? AND status='pending'",
                                           (candidate['id'],)).fetchone()
                    identity = self.db.execute('SELECT xuid FROM players WHERE name=?', (player,)).fetchone()
                    if item is None:
                        continue
                    if item['xuid'] and (not identity or item['xuid'] != identity['xuid']):
                        continue  # A different account now uses this name; keep for review.
                    command = render_queue_command(player, item['command'])
                    if not send(command):
                        break
                    ts = now_iso()
                    self.db.execute("UPDATE queue SET status='fired', fired=? WHERE id=?", (ts, item['id']))
                    self.db.execute("INSERT INTO events(ts, player, kind, detail) VALUES(?,?,?,?)",
                                    (ts, player, 'queue', f"sent: {item['command']}"[:500]))
                    delivered.append(dict(item))
                finally:
                    self.db.commit()
        return delivered

    def queue_clear_done(self) -> int:
        with self._lock:
            cur = self.db.execute("DELETE FROM queue WHERE status!='pending'")
            self.db.commit()
            return cur.rowcount

    def purge_older_than(self, days: int) -> int:
        if days < 1:
            raise ValueError('Retention must be at least one day.')
        cutoff = (datetime.now() - timedelta(days=days)) \
            .replace(microsecond=0).isoformat(sep=" ")
        with self._lock:
            cur = self.db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            self.db.commit()
            return cur.rowcount

    def close(self) -> None:
        try:
            with self._lock:
                self.db.close()
        except Exception:
            pass


def normalize_queue_command(command: str) -> str:
    if not isinstance(command, str) or any(ord(c) < 32 or ord(c) == 127 for c in command):
        raise ValueError('Enter one command without line breaks or control characters.')
    command = command.strip().removeprefix('/').strip()
    if not command or len(command) > 4096:
        raise ValueError('Enter a command of at most 4096 characters.')
    if command.startswith('!') or command.split()[0].casefold().startswith('admin:'):
        raise ValueError('Queue Bedrock console commands, not manager shortcuts or in-game-only admin: commands.')
    return command


def render_queue_command(player: str, command: str) -> str:
    """Substitute standalone targets outside quotes; leave JSON/chat literals alone."""
    command = normalize_queue_command(command)
    quoted = json.dumps(player, ensure_ascii=False)
    result, i, in_string, escaped = [], 0, False, False
    while i < len(command):
        char = command[i]
        if not in_string and (i == 0 or command[i - 1].isspace()):
            token = next((t for t in ('{player}', '@s') if command.startswith(t, i)
                          and (i + len(t) == len(command) or command[i + len(t)].isspace())), None)
            if token:
                result.append(quoted)
                i += len(token)
                continue
        if char == '"' and not escaped:
            in_string = not in_string
        escaped = char == '\\' and not escaped if in_string else False
        result.append(char)
        i += 1
    return ''.join(result)


def fmt_span(seconds: int) -> str:
    seconds = max(0, int(seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m"
    return f"{s}s"


if __name__ == "__main__":
    import sys
    h = PlayerHistory(Path(sys.argv[1] if len(sys.argv) > 1 else "."))
    print("players:")
    for p in h.players():
        flag = "online" if p["online"] else "     "
        print(f"  {flag} {p['name']:<22} playtime {p['playtime']:<10} "
              f"sessions {p['sessions']:<4} last {p['last_seen']}")
    print("\nrecent events:")
    for e in h.timeline(limit=25):
        print(f"  {e['ts']}  {e['player']:<20} {KIND_LABEL.get(e['kind'], e['kind']):<14} "
              f"{(e['detail'] or '')[:70]}")
