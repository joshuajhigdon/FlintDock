# Changelog

## Source publication — September 5, 2026

- Published the 1.3.0 launcher and companion add-on source, automated tests,
  original branding resources and standalone/installer build helpers.
- Added portable source-launch and build instructions; removed maintainer-machine
  identifiers from build-helper privacy patterns. Runtime behavior is unchanged.
- Pinned text checkouts to LF so generated add-on reference files compare
  reproducibly regardless of the machine's Git newline settings.
- Kept server data, credentials, environments, compiled caches, QA artifacts and
  machine-specific deployment/publication scripts out of the repository.
- Existing license terms remain in effect; this is source availability, not a
  change to an open-source license.

## 1.3.0 — launcher updates

- Check the public GitHub stable-release feed while the GUI is open. Checks
  default to every six hours, with one/six/twelve/twenty-four-hour choices.
- Optional automatic downloads, manual checks, progress, cancellation and
  verified-cache reuse. Automatic downloads are off by default.
- Exact release ZIP selection, SHA-256 and size verification, HTTPS restrictions
  and archive validation. No automatic extraction, installation or server stop.
- Settings, Help and Updates-page entry points. Launcher update settings/cache
  stay separate from server data and Bedrock server update settings.
- 204 automated tests passed: 156 Python, 10 packaging and 38 JavaScript. Actual
  standalone package audits and isolated frozen diagnostics passed. Targeted
  local Defender scans completed with zero detections.
- Standalone ZIP only for this version; no 1.3.0 installer was built or tested.

## 1.2.0 — persistent player management

- Searchable online/offline player directory combining known history, current
  connections and allowlist entries, with activity and queue details.
- Visitor, Member and Operator role editing for known XUIDs; guarded permission
  file writes, recovery copies and a live permission-reload request.
- Queue commands for a future join, including XUID identity checks and a
  join-time guard. Commands queued after a join wait for a later join.
- Existing history and name-only queue entries remain supported. Command
  dispatch is not an exactly-once execution guarantee across crashes.
- 177 automated tests and standalone package diagnostics passed. Installer
  lifecycle testing was blocked by an existing installation, so that candidate
  was not distributed as a verified installer.

## 1.1.1 — free-use distribution

- License now permits free use without purchase, subscription or paid activation.
- Getting-started documentation explains LootLabs-supported downloads; no ads,
  API keys, online activation or LootLabs integration were added to the app.
- Version metadata and package names updated. Existing worlds, preferences and
  in-game pack identities are not migrated or reset by this terms update.
- 155 automated tests, actual upgrades from 1.0.0 and 1.1.0, fresh installer QA,
  actual archive privacy audits and extracted-app diagnostics passed. Targeted
  local Defender scans completed with zero detections. See the
  [release notes](docs/RELEASE-NOTES.md) for details and limitations.

## 1.1.0 — FlintDock branding

- New name, icons, obsidian/violet theme and orange ignition controls.
- Original portal/flint-and-steel artwork reflects server status without an
  idle animation timer. Green health and red error indicators remain distinct.
- First-run and installer branding refreshed; smaller-window spacing refined.
- Previous server selection and Restart Manager Link pack identities preserved.

## Verification limits

Tests use disposable folders and restricted process environments on the existing
Windows 10 x64 host, not a pristine VM. Windows 11 and full-window manual visual
acceptance remain outstanding. The capture service could not capture the GUI.
Original artwork was visually inspected; automated layout/contrast tests passed
for the theme release. Newer player/update screens passed automated initialization
and layout checks; no complete manual visual acceptance is claimed.

The software and installer remain unsigned. Local Defender results are point-in-time
checks, not guarantees for other machines, future signatures, SmartScreen or
Smart App Control. A real Minecraft server is not started by the package diagnostics.
