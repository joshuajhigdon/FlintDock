"""Isolated on-host installer acceptance test. Never accesses a live server."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import winreg
import ctypes

ROOT = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--previous-installer', type=Path, help='Optional tested older installer for real upgrade QA')
args = parser.parse_args()
QA = ROOT / 'qa' / time.strftime('%Y%m%d-%H%M%S')
APP = QA / 'Installed App é'
SETUP = ROOT / 'publish/FlintDock-1.3.0-Setup.exe'
QA.mkdir(parents=True)
checks = []
report = {'environment': 'Isolated directories and restricted process environment on Windows 10 x64; not a pristine VM',
          'qa_folder': str(QA), 'checks': checks, 'ok': False}
env = {k: v for k, v in os.environ.items() if k.upper() not in
       ('PYTHONPATH','PYTHONHOME','TCL_LIBRARY','TK_LIBRARY','VIRTUAL_ENV','PYTHONSTARTUP')}
windows = Path(os.environ['SystemRoot'])
env.update(PATH=str(windows / 'System32') + ';' + str(windows), USERPROFILE=str(QA / 'Customer'),
           LOCALAPPDATA=str(QA / 'Customer/AppData/Local'), APPDATA=str(QA / 'Customer/AppData/Roaming'),
           TEMP=str(QA / 'Temp'), TMP=str(QA / 'Temp'), PYTHONNOUSERSITE='1')
for key in ('USERPROFILE','LOCALAPPDATA','APPDATA','TEMP'):
    Path(env[key]).mkdir(parents=True, exist_ok=True)


def install(folder=APP, setup=SETUP):
    return subprocess.run(f'"{setup}" /S /D={folder}', cwd=QA, env=env, timeout=45).returncode


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def uninstall():
    result = subprocess.run([str(APP / 'Uninstall.exe'), '/S'], cwd=QA, env=env, timeout=45)
    deadline = time.monotonic() + 20
    while (APP / '.launcher-install').exists() and time.monotonic() < deadline:
        time.sleep(.1)
    return result.returncode


try:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Software\\BedrockServerLauncher',
                            access=winreg.KEY_READ | winreg.KEY_WOW64_64KEY):
            raise RuntimeError('A launcher registration already exists. Refusing to change an existing installation for QA.')
    except FileNotFoundError:
        pass
    legacy_keep = {}
    if args.previous_installer:
        previous = args.previous_installer.resolve(strict=True)
        previous_versions = {'BedrockServerLauncher-1.0.0-Setup.exe': ('1.0.0', 'BedrockLauncher.exe'),
                             'FlintDock-1.1.0-Setup.exe': ('1.1.0', 'FlintDock.exe'),
                             'FlintDock-1.1.1-Setup.exe': ('1.1.1', 'FlintDock.exe')}
        assert previous.name in previous_versions
        previous_version, previous_exe = previous_versions[previous.name]
        assert install(setup=previous) == 0
        assert (APP / previous_exe).is_file()
        legacy_server = QA / 'Customer/Legacy Server'
        (legacy_server / 'worlds/Legacy World/db').mkdir(parents=True)
        legacy_world = legacy_server / 'worlds/Legacy World/db/CURRENT'
        legacy_world.write_bytes(b'legacy-world-keep-during-rebrand')
        legacy_settings = legacy_server / 'server.properties'
        legacy_settings.write_text('server-name=Legacy\nlevel-name=Legacy World\n')
        legacy_pref = Path(env['LOCALAPPDATA']) / 'BedrockServerLauncher/setup.json'
        legacy_pref.parent.mkdir(parents=True, exist_ok=True)
        legacy_pref.write_text(json.dumps({'schema': 1, 'server_folder': str(legacy_server)}))
        legacy_note = APP / '_internal/legacy-customer-note.txt'
        legacy_note.write_text('Keep unknown customer files on rebrand')
        legacy_keep = {p: digest(p) for p in (legacy_world, legacy_settings, legacy_pref, legacy_note)}
        old_gui = subprocess.Popen([str(APP / previous_exe), '--setup'], cwd=QA, env=env)
        try:
            time.sleep(2)
            assert old_gui.poll() is None
            assert install() != 0, 'Brand migration must reject a running legacy application'
            assert (APP / previous_exe).is_file()
            checks.append(f'FlintDock upgrade refuses a running v{previous_version} application')
        finally:
            old_gui.terminate()
            old_gui.wait(timeout=10)
    assert install() == 0
    if args.previous_installer:
        assert all(digest(p) == h for p, h in legacy_keep.items())
        assert not (APP / 'BedrockLauncher.exe').exists()
        assert not (APP / 'BedrockLauncherWorker.exe').exists()
        old_shortcuts = Path(env['APPDATA']) / 'Microsoft/Windows/Start Menu/Programs/Bedrock Server Launcher'
        assert not old_shortcuts.exists()
        checks.append(f'Real v{previous_version} to FlintDock 1.3.0 upgrade preserves data/preferences and updates application files/shortcuts')
    assert (APP / 'FlintDock.exe').exists(), 'Custom install location failed'
    manifest = json.loads((ROOT / 'payload-manifest.json').read_text())
    for item in manifest:
        assert digest(APP / item['path']) == item['sha256'], item['path']
    checks.append('Silent per-user install into a custom Unicode/spaced path; every payload hash matches')
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\BedrockServerLauncher',
                        access=winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
        assert winreg.QueryValueEx(key, 'DisplayVersion')[0] == '1.3.0'
        assert winreg.QueryValueEx(key, 'DisplayName')[0] == 'FlintDock'
    checks.append('Windows Apps uninstall registration and version created')
    # NSIS honors this test's redirected APPDATA when resolving Programs.
    shortcut_dir = Path(env['APPDATA']) / 'Microsoft/Windows/Start Menu/Programs/FlintDock'
    shortcuts = {'FlintDock.lnk': ('FlintDock.exe', ''),
                 'Server Setup.lnk': ('FlintDock.exe', '--setup'),
                 'Getting Started.lnk': ('_internal/customer/START-HERE.txt', ''),
                 'Uninstall.lnk': ('Uninstall.exe', '')}
    for name, (target, arguments) in shortcuts.items():
        link = shortcut_dir / name
        assert link.is_file(), name
        # Read shortcut metadata only. No Save() call and no UI automation.
        script = "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); $w = New-Object -ComObject WScript.Shell; $s = $w.CreateShortcut('" + str(link).replace("'", "''") + "'); @{Target=$s.TargetPath; Arguments=$s.Arguments} | ConvertTo-Json"
        inspected = subprocess.run([str(windows / 'System32/WindowsPowerShell/v1.0/powershell.exe'),
                    '-NoProfile', '-NonInteractive', '-Command', script], capture_output=True, text=True,
                    encoding='utf-8', errors='replace', timeout=15)
        info = json.loads(inspected.stdout)
        assert Path(info['Target']) == APP / target, info
        assert info['Arguments'] == arguments, info
    checks.append('All four Start menu shortcuts exist and resolve to the installed app/guide with correct arguments')
    result = subprocess.run([str(APP / 'FlintDockWorker.exe'), '--self-test', str(QA / 'diagnostic.json')],
        cwd=QA, env=env, capture_output=True, text=True, timeout=45)
    diagnostic = json.loads((QA / 'diagnostic.json').read_text())
    assert result.returncode == 0 and diagnostic['ok'], diagnostic
    checks.append('Installed GUI pages, SQLite/TLS, add-on resources and child workers pass with no Python/Node on PATH')
    server = QA / 'Customer/My Server'
    (server / 'worlds/My World/db').mkdir(parents=True)
    precious = server / 'worlds/My World/db/CURRENT'
    precious.write_bytes(b'customer-world-sentinel-do-not-delete')
    settings = server / 'server.properties'
    settings.write_text('server-name=Customer\nlevel-name=My World\n')
    pref = Path(env['LOCALAPPDATA']) / 'BedrockServerLauncher/setup.json'
    pref.parent.mkdir(parents=True, exist_ok=True)
    pref.write_text(json.dumps({'schema': 1, 'server_folder': str(server)}))
    unknown = APP / '_internal/customer-note.txt'
    unknown.write_text('Customer-created file: keep')
    keep = {p: digest(p) for p in (precious, settings, pref, unknown)}
    assert install(server) != 0
    assert install(server / 'Nested Application') != 0
    assert not (server / 'FlintDock.exe').exists()
    checks.append('Installer refuses existing server directory and nested server directory')
    marker = APP / '.launcher-install'
    marker.write_text('[Application]\nID=BDSL-7b3c1e42\nVersion=0.9.0\n')
    assert install() == 0
    assert all(digest(p) == h for p, h in keep.items())
    checks.append('Upgrade path from a simulated 0.9.0 marker preserves world, settings, preferences and unknown app files')
    assert install() == 0
    assert all(digest(p) == h for p, h in keep.items())
    checks.append('Same-version repair preserves customer files')
    marker.write_text('[Application]\nID=BDSL-7b3c1e42\nVersion=2.0.0\n')
    assert install() != 0
    marker.write_text('[Application]\nID=BDSL-7b3c1e42\nVersion=1.3.0\n')
    checks.append('Downgrade protection rejects a newer installation marker')
    gui = subprocess.Popen([str(APP / 'FlintDock.exe'), '--setup'], cwd=QA, env=env)
    try:
        time.sleep(2)
        assert gui.poll() is None, 'First-run GUI exited unexpectedly'
        assert install() != 0, 'Upgrade must refuse a running application'
        result = subprocess.run([str(APP / 'Uninstall.exe'), '/S'], cwd=QA, env=env, timeout=20)
        time.sleep(1)
        assert (APP / 'FlintDock.exe').exists() and marker.exists()
        checks.append('Upgrade and uninstall refuse while the installed GUI is running')
    finally:
        # Own disposable setup process only; no Minecraft server was started.
        gui.terminate()
        gui.wait(timeout=10)
    assert uninstall() == 0
    assert not (APP / 'FlintDock.exe').exists()
    assert not (APP / 'FlintDockWorker.exe').exists()
    assert all(digest(p) == h for p, h in keep.items())
    checks.append('Uninstall removes application executables and keeps all four customer-data sentinels byte-for-byte')
    assert all(digest(p) == h for p, h in legacy_keep.items() if p != pref)
    # NSIS executes a temporary child uninstaller; wait for registry cleanup too.
    deadline = time.monotonic() + 10
    while True:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Software\\BedrockServerLauncher',
                                access=winreg.KEY_READ | winreg.KEY_WOW64_64KEY):
                pass
            if time.monotonic() > deadline:
                raise AssertionError('Installation registry key remains')
            time.sleep(.1)
        except FileNotFoundError:
            checks.append('Uninstall cleans its own app registration')
            break
    assert all(not (shortcut_dir / name).exists() for name in shortcuts)
    checks.append('Uninstall removes its four Start menu shortcuts')
    report['ok'] = True
except Exception:
    import traceback
    report['error'] = traceback.format_exc()
finally:
    (ROOT / 'qa-installer.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report, indent=2))
raise SystemExit(0 if report['ok'] else 1)
