# FlintDock 1.1.1

**Ignite your world.** FlintDock is now free to use, with downloads that may be
supported through LootLabs. No purchase, subscription or paid activation is
required. There are no advertisements or LootLabs credentials inside the app.
This is a binary distribution under the included free-use terms, not open-source.

This release updates the license, getting-started documentation and version
metadata. It retains FlintDock's portal theme, 28 admin presets, optional
16-command operator add-on, searchable help, scheduling and backup tools.

## Choose your download

- **FlintDock-1.1.1-Windows-x64-Standalone.zip:** Extract All, then open
  `FlintDock/FlintDock.exe`. Keep the worker and `_internal` folder beside it.
- **FlintDock-1.1.1-Setup.exe:** per-user installation, location selection,
  Start menu shortcuts and optional desktop shortcut.
- **SHA256SUMS.txt:** verify downloaded bytes against this file.

Do not select GitHub's automatic Source code ZIP as the application download.
Minecraft server software is not included; download it from its official source.
Store your server and worlds outside the application folder.

## Verification performed September 5, 2026

- **155 automated tests passed:** 107 Python application tests, 10 packaging
  tests and 38 JavaScript add-on tests.
- Both actual extracted standalone entry points passed diagnostics with an
  isolated profile and no Python/Node on PATH, including GUI initialization,
  SQLite/TLS, bundled resources and child-worker routing.
- Fresh installation, custom Unicode paths, shortcut targets, repair, downgrade
  protection and running-process guards passed.
- Actual **1.0.0 → 1.1.1** and **1.1.0 → 1.1.1** upgrades passed. Uninstall and
  upgrades preserved disposable test worlds, settings and customer-file sentinels.
- Actual ZIP and installer payloads were audited against an explicit allowlist
  and hashes, including their embedded archives and Python code objects. No
  developer world, player data, personal settings, credentials or server binary
  is included.
- Targeted Defender scans completed with zero detections for the final installer
  and standalone package, including extracted files. Engine 1.1.26080.3,
  signatures 1.459.56.0; security protections remained enabled.

## Limitations

The application and installer are **unsigned**. A clean local scan does not
guarantee no future Defender detections, SmartScreen warnings or Smart App Control
restrictions. Do not disable antivirus or bypass organizational security rules.

Testing used isolated directories and restricted environments on an existing
Windows 10 x64 host, not a pristine VM. Windows 11 remains untested. Full-window
screen capture was unavailable; automated GUI layout/contrast tests passed.
This licensing-only release did not start a real Minecraft server. Future Bedrock
or add-on API compatibility is not guaranteed.

FlintDock is independent software, not an official Minecraft product and not
approved by or associated with Mojang or Microsoft. Runtime licenses ship with
the downloads. Free use does not grant permission to resell or redistribute the
launcher as a standalone product; see the included terms.
