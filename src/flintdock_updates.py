"""Public GitHub release downloads only. Never install, extract, or run an update."""
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from app_paths import VERSION
from bedrock_storage import atomic_json, operation_lock, zip_members

REPOSITORY = 'joshuajhigdon/FlintDock'
RELEASES_URL = f'https://github.com/{REPOSITORY}/releases'
API_URL = f'https://api.github.com/repos/{REPOSITORY}/releases/latest'
MAX_DOWNLOAD = 256 * 1024**2
MAX_UNPACKED = 512 * 1024**2
DEFAULTS = {'check_enabled': True, 'auto_download': False, 'interval_hours': 6}
DOWNLOAD_HOSTS = {'github.com', 'release-assets.githubusercontent.com',
                  'objects.githubusercontent.com', 'github-releases.githubusercontent.com'}


class UpdateError(RuntimeError):
    pass


class Cancelled(UpdateError):
    pass


def version_tuple(value):
    if not isinstance(value, str) or len(value) > 32 or not re.fullmatch(r'v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', value):
        raise UpdateError('The release needs a stable version tag such as v1.4.0.')
    return tuple(map(int, value.removeprefix('v').split('.')))


def safe_local(path):
    path = Path(path).absolute()
    if any(p.is_symlink() or getattr(p, 'is_junction', lambda: False)() for p in (path, *path.parents)):
        raise UpdateError('The update cache cannot use symbolic links or junctions.')
    return path


def storage_dir():
    from first_run import preferences_path
    return preferences_path().parent / 'FlintDockUpdates'


def read_settings(root):
    path = safe_local(Path(root) / 'settings.json')
    if not path.exists():
        return dict(DEFAULTS)
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        if (not isinstance(value, dict) or any(type(value.get(k)) is not bool for k in ('check_enabled', 'auto_download'))
                or type(value.get('interval_hours')) is not int or value['interval_hours'] not in (1, 6, 12, 24)):
            raise ValueError('Invalid update preferences')
        return {k: value[k] for k in DEFAULTS}
    except (OSError, ValueError) as exc:
        raise UpdateError('Update settings could not be read. Automatic activity is paused; save your choices to repair them.') from exc


def save_settings(root, settings):
    if (any(type(settings.get(k)) is not bool for k in ('check_enabled', 'auto_download'))
            or type(settings.get('interval_hours')) is not int or settings['interval_hours'] not in (1, 6, 12, 24)):
        raise UpdateError('Choose a supported interval and download setting.')
    atomic_json(safe_local(Path(root) / 'settings.json'), {k: settings[k] for k in DEFAULTS})


def read_state(root):
    try:
        data = json.loads(safe_local(Path(root) / 'state.json').read_text(encoding='utf-8-sig'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, UpdateError):
        return {}


def check_due(settings, state, now):
    if not settings['check_enabled']:
        return False
    try:
        last = float(state.get('last_attempt', 0))
        if not math.isfinite(last) or last < 0:
            last = 0
    except (ValueError, TypeError):
        last = 0
    # A clock rollback or invalid persisted timestamp must not suppress checks forever.
    return not last or last > now or now - last >= settings['interval_hours'] * 3600


def validate_https(url, hosts):
    parsed = urllib.parse.urlsplit(url)
    try:
        valid = (parsed.scheme == 'https' and parsed.hostname in hosts and parsed.port in (None, 443)
                 and not parsed.username and not parsed.password and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise UpdateError('GitHub returned an unexpected download address. No file was downloaded.')
    return url


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_https(newurl, DOWNLOAD_HOSTS | {'api.github.com'})
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class GitHubHTTP:
    def open(self, url):
        validate_https(url, DOWNLOAD_HOSTS | {'api.github.com'})
        headers = {'User-Agent': f'FlintDock/{VERSION}', 'Accept': 'application/octet-stream'}
        if url == API_URL:
            headers.update(Accept='application/vnd.github+json', **{'X-GitHub-Api-Version': '2026-03-10'})
        request = urllib.request.Request(url, headers=headers)
        try:
            response = urllib.request.build_opener(SafeRedirect()).open(request, timeout=15)
            validate_https(response.geturl(), DOWNLOAD_HOSTS | {'api.github.com'})
            return response
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            if code == 404:
                raise UpdateError('No public stable release is available. The repository may be private or have no published release.') from None
            if code in (403, 429):
                raise UpdateError('GitHub is limiting update requests. The launcher will wait until the next scheduled check.') from None
            raise UpdateError(f'GitHub returned HTTP {code}. Try again later.') from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise UpdateError('Could not reach GitHub securely. Check your connection and try again later.') from exc


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled('Update download/check cancelled. Nothing was installed.')


def small_download(http, url, limit, cancel):
    check_cancel(cancel)
    chunks, size, deadline = [], 0, time.monotonic() + 45
    with http.open(url) as response:
        while True:
            check_cancel(cancel)
            if time.monotonic() > deadline:
                raise UpdateError('The GitHub response took too long.')
            chunk = response.read(min(65536, limit + 1 - size))
            if not chunk:
                return b''.join(chunks)
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise UpdateError('The GitHub response exceeds the supported size limit.')


@dataclass(frozen=True)
class Release:
    version: str
    page: str
    filename: str
    url: str
    size: int
    sha256: str
    checksums_url: str = ''


def parse_release(data, current=VERSION):
    if not isinstance(data, dict) or data.get('draft') is not False or data.get('prerelease') is not False:
        raise UpdateError('Only published stable GitHub releases are supported.')
    tag = data.get('tag_name')
    version = version_tuple(tag)
    if version <= version_tuple(current):
        return None
    release_version = tag.removeprefix('v')
    filename = f'FlintDock-{release_version}-Windows-x64-Standalone.zip'
    assets = data.get('assets')
    if not isinstance(assets, list):
        raise UpdateError('This release has no downloadable assets.')
    def asset(name):
        matches = [a for a in assets if isinstance(a, dict) and a.get('name') == name]
        if not matches:
            return None
        if len(matches) != 1:
            raise UpdateError('This release has duplicate update assets.')
        result = matches[0]
        expected = f'https://github.com/{REPOSITORY}/releases/download/{tag}/{name}'
        if result.get('browser_download_url') != expected or result.get('state') != 'uploaded':
            raise UpdateError('The update asset must be fully uploaded to the configured GitHub repository.')
        return result
    package = asset(filename)
    if not package:
        raise UpdateError(f'FlintDock {release_version} exists, but its Windows standalone ZIP has not been published.')
    size = package.get('size')
    if type(size) is not int or not 0 < size <= MAX_DOWNLOAD:
        raise UpdateError('The release package size is invalid or exceeds 256 MB.')
    digest = package.get('digest') or ''
    if digest and not re.fullmatch(r'sha256:[0-9a-fA-F]{64}', digest):
        raise UpdateError('The release has an unsupported checksum.')
    sums = asset('SHA256SUMS.txt')
    if not digest and not sums:
        raise UpdateError(f'FlintDock {release_version} exists, but has no SHA-256 verification information. Download blocked.')
    return Release(release_version, f'{RELEASES_URL}/tag/{tag}', filename,
                   package['browser_download_url'], size, digest[7:].lower(),
                   sums['browser_download_url'] if sums else '')


def find_update(http=None, cancel=None, current=VERSION):
    http = http or GitHubHTTP()
    try:
        data = json.loads(small_download(http, API_URL, 2 * 1024**2, cancel))
    except (ValueError, UnicodeError) as exc:
        raise UpdateError('GitHub returned invalid release information.') from exc
    return parse_release(data, current)


def expected_digest(release, http, cancel):
    digest = release.sha256
    if release.checksums_url:
        try:
            body = small_download(http, release.checksums_url, 128 * 1024, cancel).decode('utf-8-sig')
        except UnicodeError as exc:
            raise UpdateError('The release checksum file is not valid text.') from exc
        matches = []
        for line in body.splitlines():
            match = re.fullmatch(r'([0-9a-fA-F]{64})\s+\*?(.+)', line.strip())
            if match and match[2] == release.filename:
                matches.append(match[1].lower())
        if len(matches) != 1 or (digest and digest != matches[0]):
            raise UpdateError('The release checksums are missing, duplicated or inconsistent. Download blocked.')
        digest = matches[0]
    if not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise UpdateError('A verified SHA-256 is required before downloading.')
    return digest


def validate_package(path, cancel=None):
    try:
        with zipfile.ZipFile(path) as archive:
            members = zip_members(archive, Path(path).parent / '.not-extracted', max_bytes=MAX_UNPACKED, max_files=10000)
            files = {m.filename for m in members if not m.is_dir()}
            if not {'FlintDock/FlintDock.exe', 'FlintDock/FlintDockWorker.exe'}.issubset(files):
                raise UpdateError('This ZIP is not the FlintDock standalone application.')
            if not any(name.startswith('FlintDock/_internal/') for name in files):
                raise UpdateError('The update ZIP is missing its adjacent runtime.')
            for member in members:
                check_cancel(cancel)
                if not member.filename.startswith('FlintDock/'):
                    raise UpdateError('Unexpected files outside the FlintDock application folder.')
                parts = Path(member.filename).parts
                if any(p.casefold() in ('worlds', 'backups', 'logs') for p in parts) or parts[-1].casefold() in {
                        'bedrock_server.exe', 'players.db', 'server.properties', 'permissions.json',
                        'allowlist.json', '.manager-runtime.json', 'manager_config.json', 'launcher_ui.json'}:
                    raise UpdateError('The update archive includes server data instead of only application files.')
                if member.is_dir():
                    continue
                with archive.open(member) as source:
                    while source.read(1024 * 1024):
                        check_cancel(cancel)
    except (zipfile.BadZipFile, ValueError, RuntimeError) as exc:
        if isinstance(exc, UpdateError):
            raise
        raise UpdateError('The update ZIP is damaged or contains unsafe archive paths.') from exc


def download_update(release, root, http=None, cancel=None, progress=None):
    http = http or GitHubHTTP()
    digest = expected_digest(release, http, cancel)
    root = safe_local(root)
    root.mkdir(parents=True, exist_ok=True)
    with operation_lock(root):
        target_dir = safe_local(root / 'downloads' / release.version / digest[:16])
        target_dir.mkdir(parents=True, exist_ok=True)
        target = safe_local(target_dir / release.filename)
        if target.exists():
            with target.open('rb') as existing:
                valid = hashlib.file_digest(existing, 'sha256').hexdigest() == digest
            if valid and target.stat().st_size == release.size:
                validate_package(target, cancel)
                return target
            raise UpdateError('A cached update is damaged. Remove that file from the update downloads folder, then retry.')
        if shutil.disk_usage(target_dir).free < release.size + 32 * 1024**2:
            raise UpdateError('Not enough disk space for the update download.')
        fd, temporary = tempfile.mkstemp(prefix='.flintdock-', suffix='.part', dir=target_dir)
        temporary = Path(temporary)
        received, hasher, deadline = 0, hashlib.sha256(), time.monotonic() + 300
        try:
            with os.fdopen(fd, 'wb') as output, http.open(release.url) as response:
                while True:
                    check_cancel(cancel)
                    if time.monotonic() > deadline:
                        raise UpdateError('Update download timed out. You can retry later.')
                    chunk = response.read(128 * 1024)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > release.size:
                        raise UpdateError('The download is larger than the published release asset.')
                    output.write(chunk)
                    hasher.update(chunk)
                    if progress:
                        progress(received, release.size)
                output.flush()
                os.fsync(output.fileno())
            if received != release.size or hasher.hexdigest() != digest:
                raise UpdateError('Update verification failed: size or SHA-256 mismatch. The download was discarded.')
            validate_package(temporary, cancel)
            check_cancel(cancel)
            if target.exists():
                raise UpdateError('Another update file appeared. Refresh before retrying.')
            temporary.rename(target)
            atomic_json(safe_local(target_dir / 'receipt.json'), {
                'version': release.version, 'repository': REPOSITORY, 'filename': release.filename,
                'sha256': digest, 'bytes': received, 'verified_at': time.time(), 'installed': False})
            return target
        finally:
            temporary.unlink(missing_ok=True)
