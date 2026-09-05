"""Replay the real customer ZIP through the updater with a local fake HTTP transport.

This is NOT a live GitHub download or installation test. No downloaded code runs.
"""
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'src'))
from app_paths import VERSION
import flintdock_updates as updates

package = ROOT / 'publish' / f'FlintDock-{VERSION}-Windows-x64-Standalone.zip'
digest = hashlib.sha256(package.read_bytes()).hexdigest()
base = updates.RELEASES_URL + f'/download/v{VERSION}/'
metadata = {'tag_name': f'v{VERSION}', 'draft': False, 'prerelease': False, 'assets': [
    {'name': package.name, 'size': package.stat().st_size, 'digest': f'sha256:{digest}',
     'state': 'uploaded', 'browser_download_url': base + package.name},
    {'name': 'SHA256SUMS.txt', 'state': 'uploaded', 'browser_download_url': base + 'SHA256SUMS.txt'}]}


class LocalTransport:
    def open(self, url):
        if url == updates.API_URL:
            return io.BytesIO(json.dumps(metadata).encode())
        if url == base + 'SHA256SUMS.txt':
            return io.BytesIO(f'{digest}  {package.name}\n'.encode())
        if url == base + package.name:
            return package.open('rb')
        raise AssertionError('Unexpected URL; no external HTTP is allowed in this replay.')


with tempfile.TemporaryDirectory(prefix='updater-replay-', dir=ROOT) as folder:
    client = LocalTransport()
    release = updates.find_update(client, current='0.0.0')
    result = updates.download_update(release, Path(folder), http=client)
    assert hashlib.sha256(result.read_bytes()).hexdigest() == digest
    assert not list(Path(folder).rglob('*.exe'))
    assert updates.download_update(release, Path(folder), http=client) == result
    report = {'ok': True, 'version': VERSION, 'zip_sha256': digest,
              'real_release_zip_replayed': True, 'checksum_sources_agree': True,
              'cache_reuse': True, 'extracted_or_executed': False,
              'live_github_download_tested': False}
    from bedrock_storage import atomic_json
    atomic_json(ROOT / 'qa-app-update.json', report)
    print(json.dumps(report, indent=2))
