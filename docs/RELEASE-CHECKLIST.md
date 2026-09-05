# Maintainer release checklist

This is a downloads/documentation repository. Keep application source, live servers,
worlds, build environments, credentials and private reports out of Git history.
The deny-by-default `.gitignore` deliberately allows only reviewed documentation
and branding. Do not bypass it with `git add -f`.

Local release assets are staged in `.release-assets/v1.1.1/` and are ignored by Git.
Commit/push the product documentation only; attach the binaries to a release.
No commit, push, tag, GitHub release or LootLabs link is automatically created by
this repository preparation.

## Before uploading

- Check the staged installer and ZIP match `SHA256SUMS.txt` and the build audit.
- Confirm the free-use terms are included inside both actual downloads.
- Run the package/archive privacy audit, frozen diagnostics and installer QA.
- Run a targeted Defender scan with protection enabled and verify completion.
- Keep third-party licenses. Never upload the original project/server directory.
- Retain the unsigned-build and test-limitations disclosures in release notes.
- Keep the source code private if desired; do not label this repository open-source.
- Never add API tokens to files, commit messages, URLs, screenshots or downloads.
  Rotate exposed tokens before any later integration.

## GitHub release (manual step, only after approval)

1. Review and commit the allowed repository files, then push `main`.
2. On the repository's **Releases** page, choose **Draft a new release**.
3. Use tag `v1.1.1`, title `FlintDock 1.1.1`, targeting the intended reviewed commit.
4. Attach exactly these local files:
   - `FlintDock-1.1.1-Windows-x64-Standalone.zip`
   - `FlintDock-1.1.1-Setup.exe`
   - `SHA256SUMS.txt`
5. Paste `docs/RELEASE-NOTES.md`, which includes actual results and limitations.
   Check all attachments before publishing; use a draft while reviewing.
6. After publication, download the public assets in a fresh session, verify their
   hashes and confirm the standalone ZIP—not Source code—is the app download.

These steps follow [GitHub's release documentation](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository).

## LootLabs destination (not configured yet)

After verifying a public GitHub asset, copy its actual download URL into LootLabs
as the destination. Never use a local Windows file path or a private-repository URL.
Do not claim an uncreated example URL is live. Public assets can be shared directly:
LootLabs is monetization of a link, not payment enforcement on a public file.

No token belongs in the application or repository. A future automation should keep
its credential in a separate secret store/environment, not a URL/query string or
client-side script. Do not reuse any token previously exposed in conversation.

For Planet Minecraft, verify account eligibility, ad-link rules and the AI-content
restriction before posting. Hosting on GitHub does not bypass site rules. Existing
AI-assisted branding is not approved Planet Minecraft cover artwork.
