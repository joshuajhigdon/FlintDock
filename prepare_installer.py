"""Generate exact NSIS file lists from the frozen payload, not the project tree."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
payload = ROOT / 'dist/FlintDock'
install, uninstall, manifest = [], [], []
files = sorted(p for p in payload.rglob('*') if p.is_file())
assert files and (payload / 'FlintDock.exe').is_file()
expected = set(json.loads((ROOT / 'payload-allowlist.json').read_text()))
assert {p.relative_to(payload).as_posix() for p in files} == expected, 'Unexpected installer payload'
last = None
for path in files:
    assert not path.is_symlink()
    relative = str(path.relative_to(payload))
    assert not any(c in relative for c in '$"\r\n')
    parent = str(path.parent.relative_to(payload))
    if parent != last:
        install.append('SetOutPath "$INSTDIR' + ('\\' + parent if parent != '.' else '') + '"')
        last = parent
    install.append(f'File "dist\\FlintDock\\{relative}"')
    uninstall.append(f'Delete "$INSTDIR\\{relative}"')
    manifest.append({'path': relative, 'size': path.stat().st_size,
                     'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
folders = sorted({p.parent for p in files if p.parent != payload}, key=lambda p: len(p.parts), reverse=True)
for folder in folders:
    uninstall.append(f'RMDir "$INSTDIR\\{folder.relative_to(payload)}"')
(ROOT / 'install-files.nsh').write_text('\n'.join(install) + '\n', encoding='utf-8')
(ROOT / 'uninstall-files.nsh').write_text('\n'.join(uninstall) + '\n', encoding='utf-8')
(ROOT / 'payload-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
(ROOT / 'publish').mkdir(exist_ok=True)
print(f'Exact payload: {len(files)} files, {sum(m["size"] for m in manifest):,} bytes.')
