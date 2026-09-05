"""Shared, dependency-free storage safety for the Bedrock tools."""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath


def atomic_text(path: Path, text: str) -> None:
    """Publish a complete UTF-8 file; an interrupted write keeps the old file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def atomic_json(path: Path, data) -> None:
    atomic_text(path, json.dumps(data, indent=2, ensure_ascii=False) + '\n')


def world_path(root: Path, level: str | None = None) -> Path:
    if level is None:
        level = 'Bedrock level'
        for line in (Path(root) / 'server.properties').read_text(encoding='utf-8-sig').splitlines():
            key, sep, value = line.strip().partition('=')
            if sep and key.strip() == 'level-name':
                level = value.strip()
    if (not level or level in {'.', '..'} or level.endswith((' ', '.'))
            or any(c in level for c in '/\\:<>"|?*\r\n')
            or re.fullmatch(r'(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?', level)):
        raise ValueError('level-name must be a single world folder name.')
    base = (Path(root) / 'worlds').resolve()
    target = base / level
    if target.is_symlink() or target.resolve().parent != base:
        raise ValueError('The world folder must stay inside worlds/.')
    return target


def zip_members(archive: zipfile.ZipFile, dest: Path,
                max_bytes: int = 64 * 1024**3, max_files: int = 200000) -> list:
    """Validate the whole archive before writing, including Windows aliases."""
    members = archive.infolist()
    if len(members) > max_files or sum(m.file_size for m in members) > max_bytes:
        raise ValueError('Archive exceeds the extraction size or file-count limit.')
    base = Path(dest).resolve()
    seen = set()
    for member in members:
        name = member.filename.replace('\\', '/')
        parts = name.rstrip('/').split('/')
        if (not name or name.startswith('/') or any(
                not p or p in {'.', '..'} or p.endswith((' ', '.'))
                or any(c in p for c in ':<>"|?*') or any(ord(c) < 32 for c in p)
                or re.fullmatch(r'(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?', p)
                for p in parts)):
            raise ValueError(f'Unsafe archive path: {member.filename}')
        key = '/'.join(parts).casefold()
        if key in seen:
            raise ValueError(f'Duplicate archive path: {member.filename}')
        seen.add(key)
        if stat.S_ISLNK(member.external_attr >> 16) or member.flag_bits & 1:
            raise ValueError(f'Links and encrypted entries are unsupported: {name}')
        target = base.joinpath(*PurePosixPath(name).parts).resolve()
        if not target.is_relative_to(base) or target == base:
            raise ValueError(f'Archive path escapes its destination: {name}')
    return members


def extract_zip(archive: zipfile.ZipFile, dest: Path, **limits) -> None:
    members = zip_members(archive, dest, **limits)
    for member in members:
        target = Path(dest).joinpath(*PurePosixPath(member.filename.replace('\\', '/')).parts)
        if member.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open('wb') as output:
                shutil.copyfileobj(source, output, 1024 * 1024)


@contextmanager
def operation_lock(root: Path):
    """OS lock released on exit/crash; shared by the server and maintenance tools."""
    path = Path(root) / '.bedrock-operation.lock'
    stream = path.open('a+b')
    locked = False
    try:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise RuntimeError('The server or another maintenance operation is using this folder.') from exc
        yield
    finally:
        if locked:
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_UN)
        stream.close()


def create_backup(root: Path, kind: str = 'manual', dest: Path | None = None,
                  progress=None) -> Path:
    """Caller holds operation_lock and has stopped the server."""
    world = world_path(root)
    if not world.is_dir():
        raise FileNotFoundError(f'World folder not found: {world}')
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    dest = Path(dest) if dest else Path(root) / 'backups' / f'{world.name}-{kind}-{stamp}.zip'
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.backup-', suffix='.part', dir=dest.parent)
    os.close(fd)
    try:
        files = sorted(p for p in world.rglob('*') if p.is_file())
        total = sum(p.stat().st_size for p in files)
        if shutil.disk_usage(dest.parent).free < total + 16 * 1024**2:
            raise OSError('Not enough free disk space for a safe backup.')
        with zipfile.ZipFile(temp, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
            archive.comment = json.dumps({'tool': 'bedrock', 'world': world.name,
                                          'kind': kind, 'created': stamp}).encode('utf-8')
            for i, path in enumerate(files, 1):
                if path.is_symlink() or not path.resolve().is_relative_to(world.resolve()):
                    raise ValueError(f'World contains an external link: {path.name}')
                archive.write(path, path.relative_to(world).as_posix())
                if progress and (i % 40 == 0 or i == len(files)):
                    progress(i, len(files), f'Backing up {world.name}')
        verify_backup(Path(temp))
        os.replace(temp, dest)
        return dest
    finally:
        Path(temp).unlink(missing_ok=True)


def verify_backup(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        members = zip_members(archive, Path(path).parent / '.verify-world')
        names = {m.filename.replace('\\', '/') for m in members}
        if 'level.dat' not in names or not any(n.startswith('db/') for n in names):
            raise ValueError('This archive does not contain a Bedrock world (level.dat and db/).')
        bad = archive.testzip()
        if bad:
            raise ValueError(f'Backup has damaged data: {bad}')
        return {'files': sum(not m.is_dir() for m in members),
                'bytes': sum(m.file_size for m in members)}


def restore_backup(root: Path, path: Path, progress=None) -> Path | None:
    """Validate and stage first; preserve the old world until replacement succeeds."""
    world = world_path(root)
    info = verify_backup(path)
    if shutil.disk_usage(world.parent).free < info['bytes'] * 2 + 16 * 1024**2:
        raise OSError('Not enough disk space to stage and restore the world safely.')
    stage = Path(tempfile.mkdtemp(prefix='.restore-', dir=world.parent))
    safety = None
    transaction = None
    try:
        with zipfile.ZipFile(path) as archive:
            extract_zip(archive, stage)
        if world.exists():
            safety = create_backup(root, 'replaced', progress=progress)
        from bedrock_recovery import Transaction
        transaction = Transaction(root, 'Restore world backup', progress)
        transaction.replace(world, stage)
        transaction.commit()
        return safety
    except Exception:
        if transaction:
            transaction.cancel()
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def prune_automatic(root: Path, keep: int = 10) -> list[Path]:
    """Only prune labelled automatic backups for this world; manual copies survive."""
    if keep < 1:
        return []
    candidates = []
    world = world_path(root).name
    for path in (Path(root) / 'backups').glob('*.zip'):
        try:
            with zipfile.ZipFile(path) as archive:
                data = json.loads(archive.comment)
            if data.get('tool') == 'bedrock' and data.get('world') == world and data.get('kind') == 'auto':
                candidates.append(path)
        except (OSError, ValueError, zipfile.BadZipFile):
            continue
    removed = sorted(candidates, key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)[keep:]
    for path in removed:
        path.unlink()
    return removed
