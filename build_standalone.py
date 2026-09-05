"""Build/test a direct folder ZIP, leaving existing installer and server untouched.

Run with the pinned build venv, from any working directory. No NSIS, UPX,
security setting changes, dependency downloads or automatic publication.
"""
import argparse
from datetime import datetime, timezone
from importlib.metadata import version as installed_version
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import uuid
import zipfile

from standalone_audit import Audit, digest, require, safe_member

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / 'standalone-builds'
APP_NAME = 'FlintDock'


def run(command, *, cwd=ROOT, env=None, timeout=600):
    print('Running: ' + subprocess.list2cmdline(list(map(str, command))), flush=True)
    subprocess.run(list(map(str, command)), cwd=cwd, env=env, check=True, timeout=timeout)


def prerequisites():
    require(sys.platform == 'win32' and platform.machine().upper() in {'AMD64', 'X86_64'}
            and sys.maxsize > 2**32, 'Build with Windows x64 Python.')
    require(sys.version_info[:3] == (3, 14, 7),
            'This release is validated with Python 3.14.7; review runtime licenses before changing it.')
    require(sys.prefix != sys.base_prefix, 'Use a dedicated build virtual environment.')
    for line in (ROOT / 'requirements-build.txt').read_text().splitlines():
        if line and not line.startswith('#'):
            name, wanted = line.split('==')
            require(installed_version(name) == wanted,
                    'Build dependency mismatch: ' + line + '; install requirements-build.txt.')
    for parent in (ROOT, *ROOT.parents):
        require(not parent.is_symlink() and not parent.is_junction(), 'Release root cannot use junctions.')
        require(not (parent / 'bedrock_server.exe').exists() and not (parent / 'server.properties').exists(),
                'Refusing to prepare a release inside a server installation.')
    require(not RUNS.is_symlink() and not RUNS.is_junction(), 'Build output cannot be a link/junction.')


def build():
    node = shutil.which('node')
    require(node is not None, 'Install Node.js for the add-on tests; customers do not need it.')
    label = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    job = RUNS / label
    job.mkdir(parents=True, exist_ok=False)
    run([sys.executable, '-m', 'unittest', 'discover', '-s', ROOT / 'packaging_tests', '-p', 'test_*.py'])
    run([sys.executable, '-m', 'unittest', 'discover', '-s', ROOT / 'src/tests', '-p', 'test_*.py'])
    run([node, '--experimental-vm-modules', '--test', 'tests/admin_commands.mjs'], cwd=ROOT / 'src')
    env = os.environ.copy()
    env['PYINSTALLER_CONFIG_DIR'] = str(job / 'cache')
    run([sys.executable, '-m', 'PyInstaller', '--clean', '--noconfirm',
         '--distpath', job / 'dist', '--workpath', job / 'work', ROOT / 'Launcher.spec'], env=env)
    (job / 'build.json').write_text(json.dumps({
        'schema': 1, 'tests_passed': True, 'python': platform.python_version(),
        'pyinstaller': installed_version('pyinstaller'), 'mode': 'onedir',
    }, indent=2), encoding='utf-8')
    return job


def diagnose(app, job):
    qa = job / ('qa-' + uuid.uuid4().hex[:8])
    qa.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(('PYTHON', '_PYI'))
           and k.upper() not in {'TCL_LIBRARY', 'TK_LIBRARY', 'VIRTUAL_ENV'}}
    windows = Path(os.environ['SystemRoot'])
    env.update(PATH=str(windows / 'System32') + ';' + str(windows),
               USERPROFILE=str(qa / 'Customer'), LOCALAPPDATA=str(qa / 'Customer/AppData/Local'),
               APPDATA=str(qa / 'Customer/AppData/Roaming'), TEMP=str(qa / 'Temp'), TMP=str(qa / 'Temp'))
    for key in ('USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'TEMP'):
        Path(env[key]).mkdir(parents=True, exist_ok=True)
    executables = ['FlintDock.exe', 'FlintDockWorker.exe']
    for executable in executables:
        report_path = qa / (executable + '-diagnostic.json')
        run([app / executable, '--self-test', report_path], cwd=qa, env=env, timeout=90)
        report = json.loads(report_path.read_text(encoding='utf-8'))
        require(report.get('ok') is True, 'Extracted application diagnostic failed: ' + executable)
    report['executables_tested'] = executables
    return report


def package(job):
    # Package only a build run created here, never an arbitrary server directory.
    require(job.parent == RUNS.resolve() and job.is_dir() and
            not job.is_symlink() and not job.is_junction(), 'Choose a direct standalone-builds run folder.')
    record = json.loads((job / 'build.json').read_text(encoding='utf-8'))
    require(record.get('tests_passed') is True and record.get('mode') == 'onedir', 'Missing tested build record.')
    stage = job / 'dist' / APP_NAME
    require(stage.is_dir() and not stage.is_junction() and not stage.is_symlink()
            and not stage.parent.is_junction() and not stage.parent.is_symlink(), 'Unsafe payload directory.')
    # File names are intentionally pinned to the reviewed installer payload. A
    # dependency/resource change must update the reviewed allowlist explicitly.
    expected = set(json.loads((ROOT / 'payload-allowlist.json').read_text()))
    audit = Audit()
    manifest = audit.folder(stage, expected)
    release_version = subprocess.check_output([str(stage / 'FlintDockWorker.exe'), '--version'],
                                             cwd=job, text=True, timeout=15).strip()
    require(release_version == '1.3.0', 'Update standalone version and documentation for a new version.')
    # A new packaging directory permits signing then re-packaging without ever
    # overwriting an earlier artifact. Signing must precede these hash checks.
    attempt = job / ('package-' + uuid.uuid4().hex[:8])
    attempt.mkdir()
    extracted = attempt / 'Extracted App é'
    extracted.mkdir()
    zip_name = f'{APP_NAME}-{release_version}-Windows-x64-Standalone.zip'
    candidate = attempt / (zip_name + '.candidate')
    guide = (ROOT / 'customer/START-STANDALONE.txt').read_bytes()
    audit.data('START-STANDALONE.txt', guide)
    import hashlib
    manifest['START-STANDALONE.txt'] = hashlib.sha256(guide).hexdigest()
    sums = ''.join(f'{value}  {name}\n' for name, value in sorted(manifest.items())).encode()
    with zipfile.ZipFile(candidate, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name in sorted(expected):
            archive.write(stage / name, APP_NAME + '/' + name)
        archive.writestr(APP_NAME + '/START-STANDALONE.txt', guide)
        archive.writestr(APP_NAME + '/SHA256SUMS.txt', sums)
    manifest['SHA256SUMS.txt'] = hashlib.sha256(sums).hexdigest()
    with zipfile.ZipFile(candidate) as archive:
        infos = archive.infolist()
        wanted = {APP_NAME + '/' + name for name in manifest}
        require(len(infos) == len(wanted) and {i.filename for i in infos} == wanted,
                'Final ZIP member set mismatch.')
        for item in infos:
            safe_member(item.filename)
            relative = item.filename.removeprefix(APP_NAME + '/')
            require(hashlib.sha256(archive.read(item)).hexdigest() == manifest[relative],
                    'Final ZIP hash mismatch: ' + relative)
        archive.extractall(extracted)
    final_app = extracted / APP_NAME
    final_audit = Audit()
    require(final_audit.folder(final_app, manifest) == manifest, 'Extracted payload mismatch.')
    diagnostic = diagnose(final_app, job)
    require(all(digest(final_app / name) == value for name, value in manifest.items()),
            'Application modified its installed payload during diagnostic.')
    # No customer-facing ZIP appears until archive audit and extracted-app QA pass.
    downloads = attempt / 'downloads'
    downloads.mkdir()
    archive_path = downloads / zip_name
    candidate.rename(archive_path)
    archive_hash = digest(archive_path)
    (downloads / 'SHA256SUMS.txt').write_text(f'{archive_hash}  {zip_name}\n', encoding='utf-8')
    report = {'ok': True, 'version': release_version, 'format': 'onedir ZIP; no installer',
              'file': zip_name, 'bytes': archive_path.stat().st_size, 'sha256': archive_hash,
              'payload_files': len(manifest), 'embedded_archives_inspected': final_audit.archives,
              'code_objects_inspected': final_audit.code_objects,
              'privacy_audit': 'Exact payload allowlist; private-identifier, nested archive and bytecode checks passed',
              'no_onefile_payload': True, 'diagnostic': diagnostic,
              'environment': 'Isolated folders and restricted PATH on this host; not a pristine VM',
              'antivirus_verdict': 'Not assessed by this build script',
              'signing': 'Check Authenticode status separately; this script does not sign files'}
    (attempt / 'verification.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))
    print('\nCustomer ZIP: ' + str(archive_path))
    print('Verification report: ' + str(attempt / 'verification.json'))
    print('Distribute only the downloads directory, never the release tree or QA folders.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--prepare-only', action='store_true', help='Build/test; pause for publisher signing before packaging')
    modes.add_argument('--package-run', type=Path, help='Audit/test/package an existing run after optional signing')
    args = parser.parse_args()
    prerequisites()
    job = args.package_run.absolute() if args.package_run else build()
    if args.prepare_only:
        print('Prepared: ' + str(job))
        print('Sign your two FlintDock EXEs here if you have a trusted publisher certificate: ' + str(job / 'dist' / APP_NAME))
        print('Then run: ' + subprocess.list2cmdline([sys.executable, str(Path(__file__)), '--package-run', str(job)]))
    else:
        package(job)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('Standalone build FAILED: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
