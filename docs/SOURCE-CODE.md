# FlintDock source code

This repository now includes the source for FlintDock 1.3.0. The Python launcher
and companion add-on files were taken from the clean release copy, not a live
Minecraft server. No world, player database, logs, private settings or credentials
are required to inspect or build it.

Use the **main** branch, clone this repository, or
[download main's source ZIP](https://github.com/joshuajhigdon/FlintDock/archive/refs/heads/main.zip).
The already-published v1.3.0 tag predates this source publication, so its automatic
source archive contains the older documentation-only snapshot. Existing release
tags have not been rewritten. The named standalone ZIP is the ready-to-run app.

## Where to look

- `src/BedrockLauncher.pyw`: Tk desktop launcher.
- `src/launcher_players.py`, `player_history.py`, `player_permissions.py`: online
  and offline player directory, history, roles and command queue.
- `src/flintdock_updates.py`, `launcher_app_updates.py`: GitHub update downloads
  and their settings/dialog.
- `src/server_manager.py`, `bedrock_runtime.py`: server supervision and local IPC.
- `src/addon_src/RestartManagerLink/`: companion behavior-pack manifest and scripts.
- `src/tests/`, `packaging_tests/`: Python/JavaScript tests and test-only fixtures.
- `src/branding/`, `portal_art.py`, `launcher_theme.py`: original art and theme.
- `customer/`: customer guides, existing binary terms and third-party notices.
- Root build scripts, `Launcher.spec`, `requirements-build.txt`,
  `payload-allowlist.json` and `installer.nsi`: packaging and audit inputs.

See [BUILDING.md](../BUILDING.md) for commands. Source archives do not include
Python, build environments, NSIS/7-Zip executables or Minecraft server software.

## License clarification

The source is publicly viewable, but no MIT, GPL, Apache or other open-source
license has been selected or added. Publishing it does not grant an additional
right to redistribute or sell FlintDock. The existing [end-user terms](../LICENSE.txt)
continue to apply to the distributed launcher; third-party components retain
their own licenses. Contact the maintainer if you need rights not already granted.

Do not call a source-available release “open source” unless its licensing changes.
The binary archive still does not bundle these source files; GitHub stores them
separately in this repository.

## Safe local use

Keep the checkout and build outputs separate from server data. The source GUI's
first-run setup can connect an existing server, or import your own official
Windows Bedrock ZIP after you accept Minecraft's EULA. Back up your server and
stop an old manager before switching launchers. Do not run two managers against
the same world.

Generated state, builds and real-test artifacts stay ignored. `.gitignore` is an
explicit publication allowlist: newly added source files need a reviewed allowlist
entry before they can be staged. Never force-add a live server tree, world,
secret, compiled cache or local deployment/recovery report.
