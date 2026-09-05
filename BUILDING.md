# Building and running FlintDock 1.3.0

Use a Windows x64 desktop and keep the checkout outside every server/world
directory. These instructions use relative paths from the repository root;
there is no dependency on the maintainer's folders or settings.

## Run the Python source

Install official CPython 3.14.7 x64 with Tcl/Tk. From PowerShell:

```powershell
git clone https://github.com/joshuajhigdon/FlintDock.git
Set-Location -LiteralPath '.\FlintDock'
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe .\src\BedrockLauncher.pyw
```

The GUI and server manager use Python's standard library; no pip install is needed
just to launch the source. First-run setup selects your server. You can instead
pass one server folder explicitly:

```powershell
.\.venv\Scripts\python.exe .\src\BedrockLauncher.pyw 'D:\MyBedrockServer'
```

That example is a placeholder, not an included server. Keep server data outside
this checkout, stop any old manager before switching, and accept Minecraft's
EULA yourself when importing its official ZIP. The launcher is not a server
download, hosting service or Windows service. See [source terms](docs/SOURCE-CODE.md).

## Tests and a standalone build

The validated build is CPython **3.14.7 Windows x64** with the exact package
versions in `requirements-build.txt`. Install Node.js separately and make `node`
available on PATH for the JavaScript add-on tests. Customers of the packaged app
do not need Python, Node.js or Java.

```powershell
.\.venv\Scripts\python.exe -m pip --isolated install --index-url https://pypi.org/simple --only-binary=:all: -r .\requirements-build.txt
.\.venv\Scripts\python.exe -m unittest discover -s .\src\tests -p 'test_*.py'
.\.venv\Scripts\python.exe -m unittest discover -s .\packaging_tests -p 'test_*.py'
node --experimental-vm-modules --test .\src\tests\admin_commands.mjs
.\.venv\Scripts\python.exe .\build_standalone.py
```

The builder reruns the tests itself. It creates a new unique directory under
`standalone-builds/`, uses PyInstaller's **onedir** mode with UPX off, inspects the
actual ZIP and embedded Python archives, then exercises both extracted entry
points in isolated profiles with a restricted PATH. It does not start a real
Minecraft server. Packaging stops on failed tests or an unexpected payload file.

It also refuses a checkout whose directory or any parent contains
`bedrock_server.exe` or `server.properties`. If that guard triggers, use a separate
checkout in a development folder outside that server-containing directory; do
not disable the guard or move/delete server files to make the build proceed.

A successful build prints the exact customer ZIP and adjacent verification-report
locations. Distribute only the named ZIP and its checksum attachment from that
run's `package-*/downloads/` directory—not the checkout, build tree or QA folders.
The full `FlintDock/` folder, both EXEs, `_internal/`, licenses and guides are needed.
Builds are not claimed to be byte-for-byte reproducible across machines/runs.

The pinned runtime and allowlist are deliberate. A different Python/dependency
version requires reviewing the payload, third-party licenses and tests; do not
remove the version or privacy guards merely to get a build through.

## Optional signing and security scans

This is bundled Python, not a native rewrite. Do not change to onefile, enable
UPX, add obfuscation, create antivirus exclusions or disable security protection.
Packaging cannot guarantee no Defender/SmartScreen alerts. Current builds are
unsigned; a checksum confirms bytes, not independent publisher identity.

To pause for your own publisher signing process:

```powershell
.\.venv\Scripts\python.exe .\build_standalone.py --prepare-only
```

Sign both EXEs in the printed application folder, then run the exact
`--package-run` command printed by the builder. Keys/certificates must stay out
of Git and release folders. Packaging reruns verification after signing.

To scan the actual output with Microsoft Defender, supply the exact artifact
directory printed by your build (inside this checkout) to:

```powershell
.\scan_release.ps1 -ScanPath '.\standalone-builds\YOUR-RUN\YOUR-PACKAGE'
```

The helper checks Defender completion events; scan results remain local and
ignored. Point-in-time results do not guarantee other machines or future scans.
Run clean-VM, supported-Windows-version and manual GUI acceptance tests separately.

## Optional installer sources

`installer.nsi`, `prepare_installer.py`, `audit_payload.py` and `qa_installer.py`
are included for maintainers. **No 1.3.0 installer is currently validated for
distribution.** Do not present an older installer as containing 1.3.0 features.

For future installer work, obtain official NSIS 3.12 and 7-Zip separately. Keep
NSIS's complete distribution at ignored `tools/nsis-3.12/`; its original files are
used by the installer audit. Put `7z.exe` on PATH, or set `FLINTDOCK_7ZIP` to its
absolute path. Never bundle these tool installations into the launcher payload.

Copy only the successful build's `dist/FlintDock/` runtime into a new, empty
`dist/FlintDock/` here. Then, in a disposable Windows account/VM:

```powershell
.\.venv\Scripts\python.exe .\prepare_installer.py
.\tools\nsis-3.12\makensis.exe /V2 .\installer.nsi
.\.venv\Scripts\python.exe .\audit_payload.py
.\.venv\Scripts\python.exe .\qa_installer.py
```

Installer QA installs/uninstalls the application and exercises registry/shortcuts;
it refuses an existing launcher registration. Do not run it on a live customer's
installation. Before any installer release, verify fresh install, upgrades,
downgrade/running-process guards and data-preserving uninstall in a clean test
environment. `--previous-installer` accepts an explicitly selected tested older
installer for upgrade QA. No prior installer or test world is bundled here.

## Art, add-ons and compatibility

Branding PNG/ICO/SVG assets are committed, so a normal build needs no image tools.
`build_artwork.py` can regenerate them from `src/portal_art.py`; that optional
step additionally requires Pillow, which is not in the normal build requirements.
Original artwork is not a redistribution of Minecraft textures or logos.

The opt-in `run_real_rehearsal.py` and `run_admin_rehearsal.py` scripts under
`src/tests/` require your own official server ZIP and EULA acceptance. They create
disposable QA worlds; the default test/build commands do not run them. Their
generated reports stay ignored.

Preserve the historical application-data identity `BedrockServerLauncher`,
installer marker `BDSL-7b3c1e42`, companion-pack UUIDs and command namespaces for
customer compatibility. Update version metadata, tests, build/audit expectations,
installer sources and customer guides together for a new release. Launcher
updates download a public stable ZIP; they do not install it or stop a server.
