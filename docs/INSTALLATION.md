# Installation, upgrades and removal

## Standalone ZIP

1. Download the named `FlintDock-<version>-Windows-x64-Standalone.zip` asset from
   a published release, not GitHub's automatic Source code archive.
2. In File Explorer, choose **Extract All** into a new dedicated application folder.
3. Open the extracted `FlintDock` folder and run `FlintDock.exe`. Keep
   `FlintDockWorker.exe` and the complete `_internal` folder alongside it.

The application loads its adjacent runtime. It is not a onefile application
that unpacks a runtime at each launch. The ZIP needs no installer/admin rights;
it does not create Start menu entries or Windows Apps registration.

## Installer

Run `FlintDock-<version>-Setup.exe`, choose a dedicated application directory,
and select the optional desktop shortcut if wanted. It installs for the current
user. Open **FlintDock** from Start, or **Server Setup** to choose another server.
The installer extracts application files once during installation.

## First launch

Choose an existing server or create one from your own official Windows Bedrock
ZIP. Download Minecraft server software separately from the
[official site](https://www.minecraft.net/en-us/download/server/bedrock), read its
[EULA](https://www.minecraft.net/en-us/eula), and accept it yourself.

Use a server-data folder separate from the application. The launcher does not
ship a world, players or a Minecraft executable. A new server creates its world
when started. Add your gamertag to the allowlist in Players before connecting.
Do not run two processes against the same world or ports.

The launcher does not configure your router, firewall or game-client loopback
exemption. Follow the official server's current networking guidance where needed.

## Optional in-game tools

Start the new server once, then stop it. Choose **Help → Install / update in-game
tools** and review the compatibility/settings prompts before installation.
Restart afterward. The bundled pack is still named Restart Manager Link;
its identifiers are deliberately preserved for upgrades.

Grant operator status only to trusted administrators. Some commands require
cheats and can affect achievements. **Help → Server command reference** is
read-only. **Help → Admin quick commands** provides command previews; sending
commands is a separate action. Installed-mod discovery depends on the pack's
documentation; it cannot discover every possible third-party command.

## Upgrading

Back up your server, stop every managed server and close FlintDock and its manager.
For the installer, select the existing application directory. For a standalone
ZIP, extract the new version into a new folder and point your shortcut at its EXE.
Keep server data outside both versions; do not merge runtime files across versions.

The saved server selection retains the historical
`%LOCALAPPDATA%\BedrockServerLauncher\setup.json` location. That name is expected.
Old manually created desktop shortcuts may need replacing. In-game pack UUIDs
and command namespaces remain stable; review compatibility before game updates.

## Uninstalling

Use Windows Apps or the Uninstall shortcut for an installed copy. The uninstaller
removes known app files, not external worlds, backups, settings or player history.
The saved server selection and unknown customer-created app files are retained.

For a standalone copy, stop its manager, close the app and remove only the old
extracted application folder and its shortcut after confirming your worlds are
stored elsewhere. Deleting server data is a separate, deliberate decision.

## Verification and troubleshooting

From the folder containing your downloads, use PowerShell to compare against the
release's `SHA256SUMS.txt`:

```powershell
Get-FileHash -LiteralPath '.\FlintDock-1.1.1-Windows-x64-Standalone.zip' -Algorithm SHA256
Get-FileHash -LiteralPath '.\FlintDock-1.1.1-Setup.exe' -Algorithm SHA256
```

These builds are unsigned. A checksum confirms bytes, not publisher identity or
absence of malware. Keep security protections enabled. Managed computers can
block unsigned software; consult their administrator rather than bypassing it.

For startup errors, check
`%LOCALAPPDATA%\BedrockServerLauncher\startup-error.log`; use Server Setup to correct
the server path. For server problems, inspect Console and Health. Redact logs
before sharing: they can contain player names, addresses and local paths.
