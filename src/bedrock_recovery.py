"""Durable file transactions and complete server restore points.

Callers hold bedrock_storage.operation_lock and stop the server before mutations.
The journal is published before each live change, so the next process can undo it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from bedrock_storage import atomic_json, extract_zip, world_path, zip_members

TERMINAL = {'committed', 'recovered'}
SETTINGS = ('server.properties', 'manager_config.json', 'allowlist.json', 'permissions.json',
            'packetlimitconfig.json', 'profanity_filter.wlist',
            'bedrock_version.json', 'mods/_addon_state.json', 'mods/world_disabled.json',
            'mods/profiles.json', 'bedrock_server.exe', 'bedrock_server')
TREES = ('behavior_packs', 'resource_packs', 'definitions', 'data', 'config', 'world_templates')


def timestamp():
    return datetime.now().isoformat(timespec='seconds')


def safe_target(root: Path, relative: str) -> Path:
    parts = relative.replace('\\', '/').split('/')
    if (not parts or any(p in ('', '.', '..') or ':' in p for p in parts)
            or parts[0].startswith('.operations')):
        raise ValueError(f'Invalid recovery target: {relative}')
    target = Path(root).joinpath(*parts)
    if not target.resolve().is_relative_to(Path(root).resolve()) or target.is_symlink():
        raise ValueError(f'Recovery target escapes the server: {relative}')
    return target


def remove_target(path: Path):
    if path.is_symlink():
        raise ValueError(f'Refusing to follow a link: {path}')
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def copy_target(source: Path, dest: Path):
    if source.is_symlink() or (source.is_dir() and any(p.is_symlink() for p in source.rglob('*'))):
        raise ValueError(f'Links are unsupported in recovery snapshots: {source}')
    dest.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, dest)
    else:
        shutil.copy2(source, dest)
        with dest.open('r+b') as stream:
            os.fsync(stream.fileno())


def operations(root: Path, pending_only=False) -> list[dict]:
    out = []
    for journal in (Path(root) / '.operations').glob('*/journal.json'):
        try:
            data = json.loads(journal.read_text(encoding='utf-8'))
            data['id'] = journal.parent.name
            if not pending_only or data.get('state') not in TERMINAL:
                out.append(data)
        except (ValueError, OSError):
            out.append({'id': journal.parent.name, 'state': 'unreadable', 'kind': 'Recovery journal needs inspection'})
    return sorted(out, key=lambda x: x.get('created', ''), reverse=True)


def assert_recovered(root: Path):
    pending = operations(root, True)
    if pending:
        raise RuntimeError('An interrupted operation needs recovery before starting or changing the server: ' + pending[0]['id'])


class Transaction:
    def __init__(self, root: Path, kind: str, progress=None):
        self.root = Path(root).resolve()
        assert_recovered(self.root)
        self.id = uuid.uuid4().hex
        self.path = self.root / '.operations' / self.id
        self.path.mkdir(parents=True)
        self.progress = progress
        self.data = {'format': 1, 'kind': kind, 'created': timestamp(), 'state': 'preparing', 'items': []}
        self.save()

    def save(self):
        atomic_json(self.path / 'journal.json', self.data)

    def replace(self, target: Path, source: Path | None):
        relative = Path(target).resolve().relative_to(self.root).as_posix()
        target = safe_target(self.root, relative)
        for entry in self.data['items']:
            other = Path(entry['target'])
            if Path(relative).is_relative_to(other) or other.is_relative_to(Path(relative)):
                raise ValueError('A transaction cannot contain overlapping paths.')
        index = len(self.data['items'])
        old = self.path / 'old' / str(index)
        new = self.path / 'new' / str(index)
        if target.exists():
            copy_target(target, old)
        if source is not None:
            copy_target(Path(source), new)
        self.data['items'].append({'target': relative, 'old': target.exists(),
                                    'new': source is not None, 'attempted': False})

    def write_json(self, target: Path, value):
        source = self.path / 'payload.json'
        atomic_json(source, value)
        self.replace(target, source)

    def commit(self):
        self.data['state'] = 'applying'
        self.save()
        try:
            for index, entry in enumerate(self.data['items']):
                target = safe_target(self.root, entry['target'])
                atomic_json(self.path / 'cursor.json', {'attempted': index})
                if target.is_dir():
                    remove_target(target)
                if entry['new']:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(self.path / 'new' / str(index), target)
                elif target.exists():
                    remove_target(target)
                if self.progress:
                    self.progress(index + 1, len(self.data['items']), 'Installing')
            self.data['state'] = 'committed'
            self.data['finished'] = timestamp()
            self.save()
        except Exception:
            recover(self.root, self.id)
            raise
        self.cleanup()

    def cancel(self):
        if self.data['state'] == 'preparing':
            recover(self.root, self.id)

    def cleanup(self):
        for name in ('old', 'new', 'payload.json'):
            try:
                remove_target(self.path / name)
            except OSError:
                pass


def recover(root: Path, operation_id: str):
    if not re.fullmatch(r'[a-f0-9]{32}', operation_id):
        raise ValueError('Invalid operation identifier.')
    root = Path(root).resolve()
    folder = root / '.operations' / operation_id
    if folder.is_symlink() or folder.resolve().parent != (root / '.operations').resolve():
        raise ValueError('Invalid recovery folder.')
    data = json.loads((folder / 'journal.json').read_text(encoding='utf-8'))
    if data['state'] in TERMINAL:
        return
    try:
        attempted = json.loads((folder / 'cursor.json').read_text())['attempted']
    except FileNotFoundError:
        attempted = -1
    for index in reversed(range(len(data['items']))):
        entry = data['items'][index]
        if not entry['attempted'] and index > attempted:
            continue
        target = safe_target(root, entry['target'])
        old = folder / 'old' / str(index)
        if entry['old'] and not old.exists():
            raise RuntimeError(f'Recovery snapshot is missing for {entry["target"]}; originals were kept for manual inspection.')
        # Never consume the snapshot: another interruption can rerun this recovery.
        remove_target(target)
        if entry['old']:
            copy_target(old, target)
    data['state'] = 'recovered'
    data['finished'] = timestamp()
    atomic_json(folder / 'journal.json', data)
    for name in ('old', 'new', 'payload.json'):
        try:
            remove_target(folder / name)
        except OSError:
            pass


def create_restore_point(root: Path, label='', progress=None) -> Path:
    root = Path(root).resolve()
    world = world_path(root)
    if not world.is_dir():
        raise FileNotFoundError('Create or select a world before making a restore point.')
    sources = [world] + [root / name for name in (*SETTINGS, *TREES) if (root / name).exists()]
    files = []
    for source in sources:
        files.extend(source.rglob('*') if source.is_dir() else [source])
    files = sorted(p for p in files if p.is_file())
    required = sum(p.stat().st_size for p in files)
    if shutil.disk_usage(root).free < required + 32 * 1024**2:
        raise OSError('Not enough space for a complete restore point.')
    folder = root / 'backups'
    folder.mkdir(exist_ok=True)
    path = folder / f'{world.name}-restore-point-{datetime.now():%Y%m%d-%H%M%S-%f}.zip'
    temp = path.with_suffix('.part')
    manifest = {'format': 1, 'world': world.name, 'label': label[:100], 'created': timestamp(),
                'paths': [p.relative_to(root).as_posix() for p in sources],
                'absent': [name for name in (*SETTINGS, *TREES) if not (root / name).exists()]}
    try:
        with zipfile.ZipFile(temp, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
            archive.comment = json.dumps({'tool': 'bedrock', 'kind': 'restore-point',
                                          'world': world.name, 'label': label[:100]}).encode('utf-8')
            archive.writestr('restore-point.json', json.dumps(manifest))
            for source in sources:
                if source.is_dir():
                    archive.write(source, source.relative_to(root).as_posix() + '/')
            for i, source in enumerate(files, 1):
                if source.is_symlink() or not source.resolve().is_relative_to(root):
                    raise ValueError('External links are not supported in restore points.')
                archive.write(source, source.relative_to(root).as_posix())
                if progress and (i % 50 == 0 or i == len(files)):
                    progress(i, len(files), 'Backing up restore point')
        verify_restore_point(temp)
        os.replace(temp, path)
        return path
    finally:
        temp.unlink(missing_ok=True)


def verify_restore_point(path: Path):
    with zipfile.ZipFile(path) as archive:
        members = zip_members(archive, Path(path).parent / '.restore-validation')
        data = json.loads(archive.read('restore-point.json'))
        level = data['world']
        world_path(Path(path).parent, level)
        allowed = set(SETTINGS) | set(TREES) | {f'worlds/{level}'}
        paths = data['paths']
        absent = data.get('absent', [])
        if (not isinstance(paths, list) or not isinstance(absent, list)
                or f'worlds/{level}' not in paths or not set(paths).issubset(allowed)
                or not set(absent).issubset(allowed)):
            raise ValueError('Restore point has invalid target paths.')
        if len(paths) != len(set(paths)) or set(paths) & set(absent):
            raise ValueError('Restore point has conflicting targets.')
        names = {m.filename for m in members}
        for target in paths:
            if target not in names and target + '/' not in names:
                raise ValueError(f'Restore point is missing a saved target: {target}')
        if f'worlds/{level}/level.dat' not in names or not any(n.startswith(f'worlds/{level}/db/') for n in names):
            raise ValueError('Restore point is missing its world.')
        bad = archive.testzip()
        if bad:
            raise ValueError(f'Damaged restore point: {bad}')
        return data


def restore_point(root: Path, path: Path, progress=None):
    data = verify_restore_point(path)
    safety = create_restore_point(root, 'Before restoring a complete restore point', progress)
    with tempfile.TemporaryDirectory(prefix='.restore-point-', dir=root) as temp:
        stage = Path(temp)
        with zipfile.ZipFile(path) as archive:
            extract_zip(archive, stage)
        transaction = Transaction(root, 'Restore complete point', progress)
        try:
            for relative in data['paths']:
                transaction.replace(safe_target(root, relative), stage / relative)
            for relative in data.get('absent', []):
                transaction.replace(safe_target(root, relative), None)
            transaction.commit()
        except Exception:
            transaction.cancel()
            raise
    return safety
