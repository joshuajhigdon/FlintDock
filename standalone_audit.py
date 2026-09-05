"""Fail-closed release checks; not a malware scanner or proof of benignness."""
import hashlib
import io
import marshal
from pathlib import Path, PurePosixPath
import types
import zipfile

from PyInstaller.archive.readers import CArchiveReader

# Generic guards only: never publish a maintainer's private identifier denylist.
PRIVATE_PATTERNS = (b'c:\\users\\', b'c:/users/',
                    b'-----begin private key-----', b'-----begin openssh private key-----')
BAD_PARTS = {'worlds', 'backups', 'logs', '__pycache__', '.git', 'tests'}
BAD_FILES = {'server.properties', 'permissions.json', 'allowlist.json',
             'launcher_ui.json', 'manager_config.json', '.manager-runtime.json',
             'bedrock_server.exe'}
EXES = {'FlintDock.exe', 'FlintDockWorker.exe'}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def safe_member(name):
    path = PurePosixPath(name)
    require(bool(name) and '\\' not in name and ':' not in name and
            not path.is_absolute() and '..' not in path.parts and
            not (set(part.lower() for part in path.parts) & BAD_PARTS) and
            path.name.lower() not in BAD_FILES, 'Disallowed release member: ' + name)


class Audit:
    def __init__(self):
        self.archives = 0
        self.code_objects = 0

    def scan(self, label, raw):
        low = raw.lower()
        for pattern in PRIVATE_PATTERNS:
            require(pattern not in low and pattern.decode().encode('utf-16-le') not in low,
                    'Private-identifier pattern in ' + label)

    def code(self, label, value):
        if isinstance(value, types.CodeType):
            self.code_objects += 1
            self.scan(label + ':filename', value.co_filename.encode())
            for part in value.co_consts:
                self.code(label, part)
        elif isinstance(value, str):
            self.scan(label, value.encode())
        elif isinstance(value, bytes):
            self.scan(label, value)
        elif isinstance(value, (list, tuple, frozenset)):
            for part in value:
                self.code(label, part)

    def data(self, label, raw, depth=0):
        self.scan(label, raw)
        if label.endswith('.pyc'):
            self.code(label, marshal.loads(raw[16:]))
        stream = io.BytesIO(raw)
        if zipfile.is_zipfile(stream):
            require(depth < 5, 'Unexpected nested archive depth: ' + label)
            self.archives += 1
            with zipfile.ZipFile(stream) as archive:
                names = archive.namelist()
                require(len(names) == len(set(names)), 'Duplicate ZIP entries: ' + label)
                require(sum(i.file_size for i in archive.infolist()) < 250_000_000,
                        'Unexpected archive size: ' + label)
                for name in names:
                    safe_member(name)
                    self.scan(label, name.encode())
                    self.data(label + '/' + name, archive.read(name), depth + 1)

    def executable(self, path):
        archive = CArchiveReader(str(path))
        self.archives += 1
        require('pyi-contents-directory _internal' in archive.options,
                'Expected the external _internal runtime folder')
        # b=binary, x=data, Z=ZIP, n=symlink: these would cause onefile extraction.
        # Explicitly allow only in-memory code/PYZ records for these two EXEs.
        require(all(record[-1] in {'s', 'm', 'M', 'z'} for record in archive.toc.values()),
                'Unexpected/self-extracting executable payload: ' + path.name)
        for name, record in archive.toc.items():
            self.scan(path.name, name.encode())
            if record[-1] == 'z':
                pyz = archive.open_embedded_archive(name)
                self.archives += 1
                for module in pyz.toc:
                    require(not module.startswith(('test.', 'tests.', 'pip.', 'pytest')),
                            'Development module in runtime: ' + module)
                    self.code(path.name + ':' + module, pyz.extract(module))
            else:
                self.code(path.name + ':' + name, marshal.loads(archive.extract(name)))

    def folder(self, folder, expected_paths):
        paths = list(folder.rglob('*'))
        require(not any(p.is_symlink() or p.is_junction() for p in paths),
                'Links/junctions are forbidden in customer payloads')
        files = sorted((p for p in paths if p.is_file()), key=lambda p: str(p).lower())
        actual = {p.relative_to(folder).as_posix() for p in files}
        require(actual == set(expected_paths),
                'Payload allowlist mismatch; missing=' + str(sorted(set(expected_paths) - actual)) +
                '; unexpected=' + str(sorted(actual - set(expected_paths))))
        for path in files:
            relative = path.relative_to(folder).as_posix()
            safe_member(relative)
            self.data(relative, path.read_bytes())
            if relative in EXES:
                self.executable(path)
        return {p.relative_to(folder).as_posix(): digest(p) for p in files}
