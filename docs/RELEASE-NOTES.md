# FlintDock 1.3.0

**Ignite your world.** This update adds launcher update checks and includes the
offline player-management improvements from 1.2.0. FlintDock remains free to use
under the included binary license, with no paid activation or advertisements
inside the application. It is not open-source software.

## What's new

- **Launcher updates:** open Settings → FlintDock updates. Check GitHub every
  one, six, twelve or twenty-four hours while the launcher is open. The default
  is six hours, with automatic downloads off until you enable them.
- **Verified downloads:** download manually or automatically, see progress,
  cancel, and reuse verified cached files. Size, SHA-256 and ZIP layout checks
  run before a download is marked ready. Installation is always manual; the
  updater never stops your server or runs a downloaded program.
- **Offline players:** keep known players in the directory after they leave,
  search their history, and manage their queued commands.
- **Player roles:** set Visitor, Member or Operator for players with a known
  XUID. Permission-file changes are guarded and backed up. A running compatible
  manager can request a permission reload.
- **Next-join commands:** queue commands for a later join, with identity and
  join-time safeguards. Review each command before queuing it.

The portal theme, 28 admin presets, optional 16-command operator add-on,
searchable help, restart scheduling and backup tools are retained.

## Download and upgrade

Download **FlintDock-1.3.0-Windows-x64-Standalone.zip** and **SHA256SUMS.txt**.
There is no 1.3.0 installer. Do not choose GitHub's automatic Source code archive;
it contains this documentation repository, not the application.

1. Back up your server, stop the old server/manager and close the old launcher.
2. Extract the complete ZIP into a new application folder outside your server
   and world folders. Do not merge old and new runtime files.
3. Run `FlintDock/FlintDock.exe`, keeping `FlintDockWorker.exe` and `_internal`
   beside it. Connect your existing server folder; do not create a replacement
   server over an existing world. Update any manually created shortcuts.
4. Enable automatic downloads in Settings if desired. Versions before 1.3.0
   need this manual upgrade before they gain the launcher update checker.

The saved server selection is preserved under its historical application-data
name. Worlds, player history and server settings stay in the selected server
folder. See [Installation](INSTALLATION.md) for details.

ZIP SHA-256:
`8a967203d23f75d41a1b4dc293fe6fd304e4328dfd2298b97f1eed19fb61d4e1`

Minecraft server software is not included. Download the official Windows server
separately and accept Minecraft's EULA on your own behalf. Python/Tk is bundled;
customers do not need Python, Java or Node.js installed for the launcher.

## Verification performed September 5, 2026

- **204 automated tests passed:** 156 Python application tests, 10 packaging
  tests and 38 JavaScript add-on tests, including 27 new update tests.
- Both actual packaged executables passed diagnostics in isolated profiles and
  with a restricted PATH: first-run screens, pages, player management, update
  preferences, TLS/SQLite and child-worker operation.
- The production updater downloaded the actual release ZIP using a local replay
  transport; checksum agreement, ZIP validation and cache reuse passed. No
  downloaded executable was extracted or run by the updater test.
- The actual standalone ZIP privacy audit inspected 49 payload files, seven
  embedded archives and 21,775 Python code objects. No developer world, players,
  logs, private server settings, credentials or Minecraft executable is bundled.
- Local Windows Defender scans completed for the package and application folder
  with zero detections. Engine 1.1.26080.3; signatures 1.459.56.0. Security
  protections remained enabled.

## Limitations

The application is **unsigned**. Checksums verify bytes, not independent publisher
identity. Local Defender results do not guarantee future antivirus or SmartScreen
results. Do not disable security protections to run the launcher.

Testing used disposable folders on the existing Windows 10 x64 host, not a pristine
VM. Windows 11 and a complete manual visual acceptance remain untested. No 1.3.0
installer lifecycle or real Minecraft client/server role-change test is claimed.
Future Bedrock and third-party add-on compatibility is not guaranteed.

The update feed must be publicly accessible. In-app downloads go directly to
GitHub; first-download links may remain LootLabs-supported. No private GitHub or
LootLabs token is included. At build time the public feed returned 404, so the
local replay test must not be mistaken for a verified production GitHub download.

Role changes and queued commands require appropriate server support. A submitted
command is not proof of successful execution. Queue dispatch cannot guarantee
exactly-once execution across crashes; review uncertain outcomes before retrying.

FlintDock is independent software, not an official Minecraft product, and is not
approved by or associated with Mojang or Microsoft. Runtime licenses ship with
the downloads. See the included free-use terms before redistribution.
