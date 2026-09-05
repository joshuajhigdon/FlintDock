# One-directory build: both EXEs load the adjacent _internal runtime.
# Do not remove exclude_binaries=True / COLLECT or enable UPX. Embedded PYZ
# bytecode is read in memory; it is not a onefile self-extracting runtime.
# Explicit resources only. Tests, live server data, tools and build logs are excluded.
from pathlib import Path
base = Path(SPECPATH)
workers = ['server_manager', 'bedrock_addons', 'bedrock_update',
           'build_admin_addon', 'build_mod_menu', 'launcher_health']
assets = ['manifest.json', 'scripts/main.js', 'scripts/admin.js',
          'scripts/catalog.js', 'scripts/help.js', 'scripts/reference.js']
documents = ['LICENSE.txt', 'START-HERE.txt', 'THIRD-PARTY-NOTICES.txt',
             'licenses/PYTHON.txt', 'licenses/NSIS.txt', 'licenses/PYINSTALLER.txt',
             'licenses/OPENSSL.txt', 'licenses/ZLIB.txt', 'licenses/ZLIB-NG.txt',
             'licenses/LIBTOMMATH.txt']
resources = [(str(base / 'src/command_reference.json'), '.')]
resources += [(str(base / 'src/branding/flintdock-icon.png'), 'branding')]
resources += [(str(base / 'src/addon_src/RestartManagerLink' / item),
               str(Path('addon_src/RestartManagerLink') / Path(item).parent)) for item in assets]
resources += [(str(base / 'customer' / item), str(Path('customer') / Path(item).parent)) for item in documents]
a = Analysis([str(base / 'src/BedrockLauncher.pyw')],
    pathex=[str(base / 'src')], binaries=[],
    datas=resources,
    hiddenimports=workers, hookspath=[], runtime_hooks=[],
    excludes=['pip', 'setuptools', 'pytest', 'unittest', 'test', 'idlelib', 'pydoc', 'doctest'],
    noarchive=False, optimize=1)
pyz = PYZ(a.pure)
gui = EXE(pyz, a.scripts, [], exclude_binaries=True, name='FlintDock',
          debug=False, strip=False, upx=False, console=False,
          icon=str(base / 'src/branding/flintdock.ico'),
          version=str(base / 'version_info.txt'))
worker = EXE(pyz, a.scripts, [], exclude_binaries=True, name='FlintDockWorker',
             debug=False, strip=False, upx=False, console=True,
             icon=str(base / 'src/branding/flintdock.ico'),
             version=str(base / 'version_info.txt'))
coll = COLLECT(gui, worker, a.binaries, a.datas, strip=False, upx=False, name='FlintDock')
