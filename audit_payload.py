"""Inspect actual NSIS-extracted files, ZIPs and both embedded Python archives."""
import hashlib
import io
import json
import marshal
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import zipfile
from PyInstaller.archive.readers import CArchiveReader

ROOT = Path(__file__).resolve().parent
setup = ROOT / 'publish/FlintDock-1.3.0-Setup.exe'
manifest = json.loads((ROOT / 'payload-manifest.json').read_text())
expected = {item['path'].replace('\\','/'): item for item in manifest}
# Reuse generic guards; keep maintainer-specific private checks outside Git.
from standalone_audit import PRIVATE_PATTERNS
blocked = PRIVATE_PATTERNS
bad_parts = {'worlds', 'backups', 'logs', '__pycache__', '.git', 'tests'}
bad_files = {'server.properties','permissions.json','allowlist.json','launcher_ui.json',
             'manager_config.json','.manager-runtime.json','bedrock_server.exe'}
checks = []
errors = []
code_count = 0
archive_count = 0


def scan(label, raw):
    low = raw.lower()
    for value in blocked:
        if value in low or value.decode().encode('utf-16-le') in low:
            errors.append(f'Private-identifier pattern in {label}')


def code(label, value):
    global code_count
    if isinstance(value, types.CodeType):
        code_count += 1
        scan(label + ':filename', value.co_filename.encode())
        for const in value.co_consts:
            code(label, const)
    elif isinstance(value, str):
        scan(label, value.encode())
    elif isinstance(value, bytes):
        scan(label, value)
    elif isinstance(value, (list, tuple, frozenset)):
        for part in value:
            code(label, part)


with tempfile.TemporaryDirectory(prefix='installer-audit-', dir=ROOT) as tmp:
    out = Path(tmp)
    seven_zip = os.environ.get('FLINTDOCK_7ZIP') or shutil.which('7z')
    if not seven_zip or not Path(seven_zip).is_file():
        raise RuntimeError('Install official 7-Zip and add 7z.exe to PATH, or set FLINTDOCK_7ZIP to it.')
    process = subprocess.run([seven_zip,'x',str(setup),'-o'+str(out),'-y'],
                             capture_output=True, text=True)
    assert process.returncode == 0, process.stderr
    files = [p for p in out.rglob('*') if p.is_file()]
    for p in files:
        rel = p.relative_to(out).as_posix()
        # These are NSIS-generated installer engine components, not project data.
        if rel.startswith('$PLUGINSDIR/') or rel == 'Uninstall.exe':
            assert rel in {'$PLUGINSDIR/modern-wizard.bmp', '$PLUGINSDIR/nsDialogs.dll',
                           '$PLUGINSDIR/System.dll', 'Uninstall.exe'}, rel
            if rel.startswith('$PLUGINSDIR/'):
                upstream = ROOT / 'tools/nsis-3.12'
                candidates = list(upstream.rglob('*.bmp')) if p.suffix == '.bmp' else list(upstream.rglob(p.name))
                assert any(c.read_bytes() == p.read_bytes() for c in candidates), rel
            scan(rel, p.read_bytes())
            continue
        assert rel in expected, f'Unexpected installer file: {rel}'
        item = expected[rel]
        assert hashlib.sha256(p.read_bytes()).hexdigest() == item['sha256'], rel
        assert not set(p.relative_to(out).parts) & bad_parts, rel
        assert p.name not in bad_files, rel
        raw = p.read_bytes()
        scan(rel, raw)
        if zipfile.is_zipfile(p):
            archive_count += 1
            with zipfile.ZipFile(p) as z:
                for name in z.namelist():
                    scan(rel + '/' + name, name.encode())
                    data = z.read(name)
                    scan(rel + '/' + name, data)
                    if name.endswith('.pyc'):
                        code(rel + '/' + name, marshal.loads(data[16:]))
        if p.name in {'FlintDock.exe','FlintDockWorker.exe'}:
            pkg = CArchiveReader(str(p))
            archive_count += 1
            for name, record in pkg.toc.items():
                scan(rel + ':' + name, name.encode())
                if record[-1] == 'z':
                    pyz = pkg.open_embedded_archive(name)
                    archive_count += 1
                    for module in pyz.toc:
                        assert not module.startswith(('test.', 'tests.', 'pip.', 'pytest')), module
                        code(rel + ':' + module, pyz.extract(module))
                elif record[-1] in ('s','m','M'):
                    code(rel + ':' + name, marshal.loads(pkg.extract(name)))
                else:
                    scan(rel + ':' + name, pkg.extract(name))
    actual = {p.relative_to(out).as_posix() for p in files}
    assert set(expected).issubset(actual)
    checks.append(f'{len(expected)} actual installer payload files match allowlisted SHA-256 manifest')
    checks.append(f'{archive_count} embedded archives and {code_count} Python code objects inspected')
    checks.append('No world, backup, player database, log, personal config or Minecraft executable files present')
    checks.append('Private machine identifiers absent from payloads, decompressed bytecode constants and filenames')
report = {'ok': not errors, 'installer': setup.name, 'sha256': hashlib.sha256(setup.read_bytes()).hexdigest(),
          'bytes': setup.stat().st_size, 'checks': checks, 'errors': errors}
(ROOT / 'audit-installer.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report, indent=2))
raise SystemExit(0 if report['ok'] else 1)
