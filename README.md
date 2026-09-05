<p align="center"><img src="assets/flintdock-icon.png" width="112" height="112" alt="FlintDock portal and spark icon"></p>

# FlintDock

**Ignite your world.** A free-to-use Windows desktop launcher and administration
tool for Minecraft Bedrock Dedicated Server.

Obsidian panels, portal-violet accents and an ignition-themed dashboard bring
server controls, health checks, add-ons and admin tools into one interface.
This repository includes the launcher source, companion add-on source, tests,
build helpers and product documentation. Ready-to-run downloads are Release assets.

## Downloads

Open the [Releases page](https://github.com/joshuajhigdon/FlintDock/releases) and
choose an attached release asset when available:

- **Standalone ZIP:** extract the complete folder, then run `FlintDock.exe`.
- **Windows installer:** choose an application folder and use the Start menu shortcuts.
- **SHA256SUMS.txt:** checksums for verifying the downloaded files.

**Version 1.3.0 is a standalone ZIP release; there is no 1.3.0 installer.**
See the [1.3.0 release notes](docs/RELEASE-NOTES.md) for features and testing.

GitHub's automatically generated **Source code** ZIP/tar.gz snapshots its tag,
not a ready-to-run EXE. The existing v1.3.0 tag predates source publication;
use [main's source ZIP](https://github.com/joshuajhigdon/FlintDock/archive/refs/heads/main.zip)
or clone main for the application source. Most users should download the named
FlintDock standalone asset instead.
Do not copy or run an EXE by itself; its companion runtime files are required.

FlintDock is free to use under the [included terms](LICENSE.txt). Downloads may
be supported through LootLabs links. The launcher contains no advertising,
LootLabs API key, paid activation or LootLabs account requirement. A free-use
binary license is not an open-source license.

## Source and building

Browse [the Python launcher](src/BedrockLauncher.pyw), [the companion add-on](src/addon_src/RestartManagerLink),
or [the tests](src/tests). See [Source code](docs/SOURCE-CODE.md) for the layout and
license clarification, and [BUILDING.md](BUILDING.md) for source-launch, test and
standalone-build commands. Keep this checkout separate from your server and worlds.

## Features

- Start, stop and restart a Bedrock server, with a state-aware portal dashboard.
- View console output, server health, players and activity history.
- Keep offline players visible, review their history, queue commands for a later
  join, and change Visitor/Member/Operator roles for players with known XUIDs.
- Check GitHub for launcher updates while FlintDock is open, with optional
  automatic, checksum-verified downloads. Installation remains manual.
- Configure restart schedules and manage backups and server settings.
- Manage add-ons and install/update the bundled in-game helper pack.
- Use **28 admin quick-command presets**, with search and a command preview.
- Browse read-only help for core server commands, the added admin commands and
  discoverable installed-pack documentation.
- Use **16 operator-only in-game commands** through the optional companion add-on.

Some commands require cheats or the companion add-on. Not every third-party pack
documents its commands, and future Bedrock/API versions are not guaranteed.
The launcher is a local desktop application, not a hosting service or web panel.
Queued commands require a running manager and a matching player join; sending a
command is not proof that Minecraft accepted it. Only trust administrators with
operator permissions. Automatic update downloads require a public GitHub release.

## Getting started

1. Download a named release asset and install it or extract the entire ZIP.
2. Open FlintDock and connect an existing server, or import your own official
   [Windows Bedrock server ZIP](https://www.minecraft.net/en-us/download/server/bedrock).
3. Choose a server-data folder **outside the application folder**. Review and
   accept Minecraft's EULA yourself.
4. Select **Ignite server**. New servers use an allowlist; add yourself in Players.

See the [installation and upgrade guide](docs/INSTALLATION.md).

## Requirements and safeguards

- Windows x64 desktop. Windows 10 22H2 was tested locally; Windows 11 remains
  untested. Use a security-maintained operating system. No macOS/Linux/ARM build.
- Python/Tk is bundled; customers do not need Python, Java or Node.js installed.
- Minecraft server software, game access, hosting and worlds are not included.
- Store worlds/settings outside the app directory and keep independent backups.
- The manager is not a Windows service and does not automatically survive a reboot.

The builds are currently **unsigned**. Verify the download source and checksum;
do not disable antivirus or bypass organizational security policies. Local tests
and a clean local scan do not guarantee future antivirus or SmartScreen results.
See the [changelog and test limitations](CHANGELOG.md).

## Privacy and licensing

Release packages exclude the developer's world, backups, players, logs, personal
settings and credentials. Report issues without sharing private paths, addresses,
player data or tokens. The original-art branding was developed with AI assistance.
Screenshots/artwork do not imply official affiliation or third-party site approval.

Third-party runtime licenses and notices are included with the downloads.
FlintDock is independent software, **not an official Minecraft product** and not
approved by or associated with Mojang or Microsoft.
