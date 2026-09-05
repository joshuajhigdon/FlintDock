"""Run an update against a disposable copy, with separate ports and no allowed players."""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import queue
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import zipfile

from bedrock_storage import atomic_json, atomic_text, extract_zip, world_path
from bedrock_experience import properties, settings_preview, redact
from bedrock_recovery import copy_target


def spare_port(family=socket.AF_INET):
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        sock.bind(('127.0.0.1', 0) if family == socket.AF_INET else ('::1', 0))
        return sock.getsockname()[1]


def rehearse(root: Path, archive: Path, version: str, progress=None, cancel=None,
              timeout=120, settle=8, command=None):
    """Caller holds operation_lock. command is an injectable test driver only."""
    root = Path(root).resolve()
    from bedrock_update import server_running
    if server_running():
        raise RuntimeError('Stop the server before copying its world for a rehearsal.')
    world = world_path(root)
    folder = root / 'rehearsals'
    folder.mkdir(exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.rehearsal-', dir=root))
    report = {'version': version, 'created': datetime.now().isoformat(timespec='seconds'),
              'started': False, 'clean_exit': False, 'passed': False, 'output': []}
    proc = None
    reader = None
    try:
        if progress:
            progress(0, 0, 'Preparing isolated server copy')
        with zipfile.ZipFile(archive) as zipped:
            required = sum(m.file_size for m in zipped.infolist())
            required += sum(p.stat().st_size for p in world.rglob('*') if p.is_file())
            if shutil.disk_usage(root).free < required * 2 + 32*1024**2:
                raise OSError('Not enough disk space for an isolated rehearsal.')
            extract_zip(zipped, stage)
        copy_target(world, stage / 'worlds' / world.name)
        for name in ('behavior_packs', 'resource_packs', 'config'):
            source = root / name
            if source.exists():
                if source.is_symlink() or any(p.is_symlink() for p in source.rglob('*')):
                    raise ValueError('Links are unsupported in rehearsal inputs.')
                # Match in-place updating: custom packs survive; newer bundled files win.
                target = stage / name
                for path in source.rglob('*'):
                    if path.is_file() and not (target / path.relative_to(source)).exists():
                        dest = target / path.relative_to(source)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, dest)
        if cancel and cancel.is_set():
            raise RuntimeError('Rehearsal cancelled before starting.')
        text = (root / 'server.properties').read_text(encoding='utf-8-sig')
        ipv4, ipv6 = spare_port(), spare_port(socket.AF_INET6)
        while ipv6 == ipv4:
            ipv6 = spare_port(socket.AF_INET6)
        props, _ = settings_preview(text, {'server-name': 'Isolated update rehearsal',
            'level-name': world.name, 'server-port': str(ipv4), 'server-portv6': str(ipv6),
            'enable-lan-visibility': 'false', 'allow-list': 'true', 'online-mode': 'true',
            'content-log-console-output-enabled': 'true', 'emit-server-telemetry': 'false'})
        atomic_text(stage / 'server.properties', props)
        atomic_json(stage / 'allowlist.json', [])
        atomic_json(stage / 'permissions.json', [])
        report['ports'] = [ipv4, ipv6]
        exe = stage / ('bedrock_server.exe' if os.name == 'nt' else 'bedrock_server')
        if not exe.exists() and not command:
            raise ValueError('The selected archive has no server executable.')
        if progress:
            progress(0, 0, 'Testing server startup and pack loading')
        proc = subprocess.Popen(command or [str(exe)], cwd=stage, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            env={**os.environ, 'LD_LIBRARY_PATH': str(stage)})
        output = queue.Queue()
        def read_output():
            for line in proc.stdout:
                output.put(line.rstrip())
        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        start = time.monotonic()
        ready_at = None
        while time.monotonic()-start < timeout:
            try:
                line = output.get(timeout=.1)
                report['output'].append(redact(line))
                if 'Server started.' in line:
                    report['started'] = True
                    ready_at = time.monotonic()
            except queue.Empty:
                pass
            if proc.poll() is not None or (ready_at and time.monotonic()-ready_at >= settle):
                break
            if cancel and cancel.is_set():
                report['cancelled'] = True
                break
        if proc.poll() is None:
            if progress:
                progress(0, 0, 'Verifying clean shutdown')
            proc.stdin.write('stop\n')
            proc.stdin.flush()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
                report['forced_stop'] = True
        reader.join(timeout=3)
        while not output.empty():
            report['output'].append(redact(output.get_nowait()))
        report['clean_exit'] = proc.returncode == 0 and not report.get('forced_stop')
        report['errors'] = [line for line in report['output'] if (' ERROR]' in line or ' FATAL]' in line)
                            and 'No targets matched selector' not in line]
        report['passed'] = report['started'] and report['clean_exit'] and not report['errors'] and not report.get('cancelled')
    except Exception as exc:
        report['failure'] = str(exc)
    finally:
        if proc and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        if reader:
            reader.join(timeout=3)
        if proc:
            if proc.stdin:
                proc.stdin.close()
            if proc.stdout:
                proc.stdout.close()
        if stage.resolve().parent != root or not stage.name.startswith('.rehearsal-'):
            raise RuntimeError('Refusing to remove an unexpected rehearsal path.')
        shutil.rmtree(stage)
        report['output'] = report['output'][-500:]
        report_path = folder / f'rehearsal-{datetime.now():%Y%m%d-%H%M%S-%f}.json'
        atomic_json(report_path, report)
    return report, report_path
