"""Detached Bedrock manager transport. JSON RPC binds to loopback only.

The random bearer token is local to the server folder. No pickle, remote bind,
firewall change, Windows service installation, or automatic startup registration.
"""
from __future__ import annotations

from collections import deque
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from bedrock_storage import atomic_json

DESCRIPTOR = '.manager-runtime.json'


def rpc(root: Path, method: str, **args):
    descriptor = json.loads((Path(root) / DESCRIPTOR).read_text(encoding='utf-8'))
    port = int(descriptor['port'])
    if not 1 <= port <= 65535:
        raise ValueError('Invalid manager endpoint.')
    request = urllib.request.Request(f'http://127.0.0.1:{port}/rpc',
        data=json.dumps({'method': method, **args}).encode('utf-8'),
        headers={'Authorization': 'Bearer ' + descriptor['token'], 'Content-Type': 'application/json'})
    # Ignore proxy environment variables for local process control.
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=1.5) as response:
        result = json.load(response)
    if not result.get('ok'):
        raise RuntimeError(result.get('error', 'Manager request failed'))
    return result


class Runtime:
    def __init__(self, manager):
        self.manager = manager
        self.root = manager.root
        self.token = secrets.token_hex(32)
        self.id = secrets.token_hex(12)
        self.events = deque(maxlen=6000)
        self.cursor = 0
        self.lock = threading.Lock()
        self.logger = logging.getLogger('bedrock-runtime-' + self.id)
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.handler = RotatingFileHandler(self.root / 'server_manager.log', maxBytes=5*1024**2,
                                          backupCount=5, encoding='utf-8')
        self.handler.setFormatter(logging.Formatter('%(asctime)s %(message)s', datefmt='%Y-%m-%dT%H:%M:%S'))
        self.logger.addHandler(self.handler)
        runtime = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                if self.path != '/rpc' or not hmac.compare_digest(
                        self.headers.get('Authorization', ''), 'Bearer ' + runtime.token):
                    self.send_error(403)
                    return
                try:
                    length = int(self.headers.get('Content-Length', 0))
                    if not 0 < length <= 65536:
                        raise ValueError('Invalid request size')
                    self.connection.settimeout(2)
                    payload = json.loads(self.rfile.read(length))
                    result = runtime.handle(payload)
                except Exception as exc:
                    result = {'ok': False, 'error': str(exc)}
                body = json.dumps(result).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.http.daemon_threads = True

    def start(self):
        atomic_json(self.root / DESCRIPTOR, {'port': self.http.server_port, 'token': self.token,
                                            'pid': os.getpid(), 'id': self.id})
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def emit(self, line):
        line = str(line).rstrip('\r\n')[:32768]
        if not line:
            return
        self.logger.info(line)
        with self.lock:
            self.cursor += 1
            self.events.append((self.cursor, line))

    def handle(self, request):
        method = request.get('method')
        if method in ('status', 'poll'):
            with self.lock:
                cursor = max(0, int(request.get('cursor', 0)))
                events = list(self.events)
                selected = [e for e in events if e[0] > cursor][:300]
                result = {'ok': True, 'id': self.id, 'state': self.manager.snapshot(),
                          'events': selected if method == 'poll' else [],
                          'cursor': selected[-1][0] if selected else self.cursor,
                          'gap': bool(events and cursor and cursor < events[0][0]-1)}
            return result
        if method == 'send':
            command = str(request.get('command', '')).strip()
            if not command or len(command) > 4096 or '\n' in command or '\r' in command:
                raise ValueError('Send one console command at a time.')
            if command.startswith('!'):
                self.manager.commands.put(command)
                return {'ok': True}
            return {'ok': self.manager.server.send(command), 'error': 'The server is not ready.'}
        if method == 'player_role':
            from player_permissions import set_player_role
            with self.manager.state_lock:
                if self.manager.stopping.is_set() or self.manager.maintenance or not self.manager.server_up:
                    raise RuntimeError('Wait until the server is running and no maintenance is in progress.')
                result = set_player_role(self.root, request.get('xuid'), request.get('role'),
                                         request.get('revision'))
                # Saving the override is confirmed; applying it live is not.
                result['reload_sent'] = bool(self.manager.server.send('permission reload'))
                return {'ok': True, **result}
        raise ValueError('Unknown manager operation.')

    def close(self):
        self.http.shutdown()
        self.http.server_close()
        self.handler.close()
        self.logger.removeHandler(self.handler)
        try:
            path = self.root / DESCRIPTOR
            if json.loads(path.read_text())['id'] == self.id:
                path.unlink()
        except (FileNotFoundError, ValueError):
            pass


class EventOutput:
    encoding = 'utf-8'
    def __init__(self, runtime):
        self.runtime = runtime
        self.buffers = threading.local()
    def write(self, text):
        value = getattr(self.buffers, 'value', '') + text
        parts = value.split('\n')
        self.buffers.value = parts.pop()
        for line in parts:
            self.runtime.emit(line)
        return len(text)
    def flush(self):
        pass


def serve(manager):
    runtime = Runtime(manager)
    manager.runtime = runtime
    before = sys.stdout, sys.stderr
    try:
        sys.stdout = sys.stderr = EventOutput(runtime)
        runtime.start()
        return manager.run()
    finally:
        sys.stdout, sys.stderr = before
        runtime.close()


class ManagerClient:
    """UI adapter with cached status; network work runs on its reader thread."""
    detached = True
    def __init__(self, root: Path, out_queue):
        self.root, self.q = Path(root), out_queue
        self.proc = None
        self.connected = False
        self.state = {}
        self._stop_reader = threading.Event()
        self._reader = None
        self._starting = False
        self._cursor = 0
        self._runtime_id = None

    def running(self):
        return self.connected or self._starting

    def attach(self):
        if self._reader and self._reader.is_alive():
            return
        self._stop_reader.clear()
        self._reader = threading.Thread(target=self._poll, daemon=True)
        self._reader.start()

    def _poll(self):
        failures = 0
        while not self._stop_reader.is_set():
            try:
                result = rpc(self.root, 'poll', cursor=self._cursor)
                if self._runtime_id != result['id']:
                    self._runtime_id = result['id']
                    self._cursor = 0
                    result = rpc(self.root, 'poll', cursor=0)
                self.connected = True
                self._starting = False
                failures = 0
                self.state = result['state']
                for cursor, line in result['events']:
                    self.q.put(('runtime_line', line))
                self._cursor = result['cursor']
                self.q.put(('runtime_state', self.state))
                if result['gap']:
                    self.q.put(('line', '[launcher] Earlier output is available in the rotated server logs.'))
            except (OSError, ValueError, RuntimeError, urllib.error.URLError):
                failures += 1
                if failures >= 3 and self.connected:
                    self.connected = False
                    self.q.put(('exit', ''))
                if self._starting and self.proc and self.proc.poll() is not None:
                    self._starting = False
                    self.q.put(('error', 'The manager could not start. Check launcher_runtime.log and Recovery.'))
                elif self._starting and time.monotonic() - self._start_time > 30:
                    self._starting = False
                    self.q.put(('error', 'Manager connection timed out. Its process was kept alive; check launcher_runtime.log before trying again.'))
            self._stop_reader.wait(.35 if self.connected else 1)

    def start(self):
        if self.running():
            return
        try:
            result = rpc(self.root, 'status')
            self.connected = True
            self.state = result['state']
            self.attach()
            return
        except (OSError, ValueError, RuntimeError):
            pass
        from bedrock_recovery import assert_recovered
        assert_recovered(self.root)
        from app_paths import worker_command
        flags = getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
        # Only startup failures reach this file; normal output is rotated by Runtime.
        with (self.root / 'launcher_runtime.log').open('ab') as log:
            self.proc = subprocess.Popen(worker_command('server_manager', '--daemon', '--server', self.root),
                cwd=self.root, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                env={**os.environ, 'PYTHONIOENCODING': 'utf-8'}, creationflags=flags,
                start_new_session=os.name != 'nt', close_fds=True)
        self._starting = True
        self._start_time = time.monotonic()
        self.attach()

    def send(self, text):
        try:
            return rpc(self.root, 'send', command=text)['ok']
        except (OSError, ValueError, RuntimeError):
            return False

    def shutdown(self, wait=120):
        if not self.send('!quit'):
            return
        end = time.monotonic() + wait
        while self.running() and time.monotonic() < end:
            time.sleep(.2)
        if self.running():
            self.q.put(('error', 'The server is still saving or stopping. Its manager was kept alive.'))

    def disconnect(self):
        self._stop_reader.set()
        if self._reader:
            self._reader.join(timeout=2)
        self.connected = False
