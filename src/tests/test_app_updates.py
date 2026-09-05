"""Updater tests use synthetic release metadata, in-memory responses and temp files."""
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request
import zipfile

from test_tooling import Isolated, fixture, load_launcher
import flintdock_updates as updates
from launcher_app_updates import AppUpdateController


def sample_zip(extra=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in {'FlintDock/FlintDock.exe': b'MZ-demo-gui',
                              'FlintDock/FlintDockWorker.exe': b'MZ-demo-worker',
                              'FlintDock/_internal/python314.dll': b'MZ-demo-runtime', **(extra or {})}.items():
            archive.writestr(name, content)
    return output.getvalue()


def release_data(raw, version='1.4.0', digest=True, sums=False):
    name = f'FlintDock-{version}-Windows-x64-Standalone.zip'
    base = f'https://github.com/{updates.REPOSITORY}/releases/download/v{version}/'
    asset = {'name': name, 'browser_download_url': base + name, 'size': len(raw), 'state': 'uploaded'}
    if digest:
        asset['digest'] = 'sha256:' + hashlib.sha256(raw).hexdigest()
    assets = [asset]
    if sums:
        assets.append({'name': 'SHA256SUMS.txt', 'browser_download_url': base + 'SHA256SUMS.txt', 'state': 'uploaded'})
    return {'tag_name': 'v' + version, 'draft': False, 'prerelease': False, 'assets': assets}


class FakeHTTP:
    def __init__(self, payload, raw, sums=None):
        self.payload, self.raw, self.sums = payload, raw, sums
        self.calls = []

    def open(self, url):
        self.calls.append(url)
        if url == updates.API_URL:
            return io.BytesIO(json.dumps(self.payload).encode())
        if url.endswith('SHA256SUMS.txt'):
            return io.BytesIO(self.sums or b'')
        return io.BytesIO(self.raw)


class UpdateDataTests(Isolated):
    def setUp(self):
        super().setUp()
        self.cache = self.root / 'Update Cache'
        self.raw = sample_zip()
        self.payload = release_data(self.raw)
        self.http = FakeHTTP(self.payload, self.raw)
        self.release = updates.parse_release(self.payload, '1.3.0')

    def test_semver_uses_numbers_not_alphabetical_order(self):
        self.assertGreater(updates.version_tuple('v1.10.0'), updates.version_tuple('1.9.9'))
        for invalid in ('1.2', '1.2.3-rc1', '../1.4.0', 'v01.2.3', None, 5):
            with self.assertRaises(updates.UpdateError):
                updates.version_tuple(invalid)

    def test_equal_or_older_releases_do_not_offer_downgrades(self):
        self.assertIsNone(updates.parse_release(self.payload, '1.4.0'))
        self.assertIsNone(updates.parse_release(self.payload, '2.0.0'))

    def test_drafts_and_prereleases_are_never_downloaded(self):
        for key in ('draft', 'prerelease'):
            with self.assertRaises(updates.UpdateError):
                updates.parse_release({**self.payload, key: True}, '1.3.0')

    def test_exact_repo_asset_required_not_source_zip_or_installer(self):
        for change in ({'name': 'Source code.zip'}, {'name': 'FlintDock-1.4.0-Setup.exe'},
                       {'browser_download_url': 'https://github.com/another/repo/releases/download/v1.4.0/update.zip'},
                       {'state': 'new'}):
            data = {**self.payload, 'assets': [{**self.payload['assets'][0], **change}]}
            with self.assertRaises(updates.UpdateError):
                updates.parse_release(data, '1.3.0')

    def test_oversized_duplicate_and_unverified_assets_blocked(self):
        for value in (0, -1, True, updates.MAX_DOWNLOAD + 1):
            data = {**self.payload, 'assets': [{**self.payload['assets'][0], 'size': value}]}
            with self.assertRaises(updates.UpdateError):
                updates.parse_release(data, '1.3.0')
        with self.assertRaises(updates.UpdateError):
            updates.parse_release({**self.payload, 'assets': self.payload['assets'] * 2}, '1.3.0')
        with self.assertRaises(updates.UpdateError):
            updates.parse_release(release_data(self.raw, digest=False), '1.3.0')

    def test_checksums_file_fallback_and_consistency(self):
        payload = release_data(self.raw, digest=False, sums=True)
        release = updates.parse_release(payload, '1.3.0')
        digest = hashlib.sha256(self.raw).hexdigest()
        http = FakeHTTP(payload, self.raw, f'{digest}  {release.filename}\n'.encode())
        self.assertEqual(updates.expected_digest(release, http, None), digest)
        http.sums = f'{"0"*64}  {release.filename}\n'.encode()
        with self.assertRaises(updates.UpdateError):
            updates.expected_digest(replace(release, sha256=digest), http, None)
        http.sums = (f'{digest}  {release.filename}\n' * 2).encode()
        with self.assertRaises(updates.UpdateError):
            updates.expected_digest(release, http, None)

    def test_only_https_github_hosts_and_no_userinfo(self):
        for url in ('http://github.com/test', 'https://github.com.evil.example/test',
                    'https://user:secret@github.com/test', 'https://github.com:444/test',
                    'https://127.0.0.1/test', 'file:///C:/test', 'https://github.com/test#fragment'):
            with self.assertRaises(updates.UpdateError):
                updates.validate_https(url, updates.DOWNLOAD_HOSTS)
        self.assertEqual(updates.validate_https('https://release-assets.githubusercontent.com/file?sig=x', updates.DOWNLOAD_HOSTS),
                         'https://release-assets.githubusercontent.com/file?sig=x')

    def test_redirect_is_validated_before_it_is_followed(self):
        redirect = updates.SafeRedirect()
        request = urllib.request.Request(self.release.url)
        with self.assertRaises(updates.UpdateError):
            redirect.redirect_request(request, None, 302, '', {}, 'https://example.com/untrusted.zip')

    def test_public_api_request_has_no_credentials_and_clear_http_errors(self):
        for code, fragment in ((404, 'No public stable'), (403, 'limiting'), (429, 'limiting'), (500, 'HTTP 500')):
            error = urllib.error.HTTPError(updates.API_URL, code, 'Test', {}, None)
            opener = Mock()
            opener.open.side_effect = error
            with patch('flintdock_updates.urllib.request.build_opener', return_value=opener), self.assertRaisesRegex(updates.UpdateError, fragment):
                updates.GitHubHTTP().open(updates.API_URL)
            request = opener.open.call_args.args[0]
            self.assertNotIn('Authorization', dict(request.header_items()))

    def test_invalid_and_oversized_api_json_rejected(self):
        http = Mock()
        http.open.return_value = io.BytesIO(b'bad json')
        with self.assertRaises(updates.UpdateError):
            updates.find_update(http)
        http.open.return_value = io.BytesIO(b'x' * 50)
        with self.assertRaises(updates.UpdateError):
            updates.small_download(http, updates.API_URL, 10, None)

    def test_verified_download_reused_without_extracting_or_running(self):
        before = (self.world / 'level.dat').read_bytes()
        result = updates.download_update(self.release, self.cache, self.http)
        self.assertEqual(result.read_bytes(), self.raw)
        receipt = json.loads((result.parent / 'receipt.json').read_text())
        self.assertFalse(receipt['installed'])
        calls = len(self.http.calls)
        self.assertEqual(updates.download_update(self.release, self.cache, self.http), result)
        self.assertEqual(len(self.http.calls), calls)
        self.assertEqual(list(self.cache.rglob('*.exe')), [])
        self.assertEqual((self.world / 'level.dat').read_bytes(), before)

    def test_checksum_and_length_mismatch_discard_partial_file(self):
        for raw in (self.raw[:-1], self.raw + b'oversize', self.raw[:-1] + bytes([self.raw[-1] ^ 1])):
            http = FakeHTTP(self.payload, raw)
            with self.assertRaises(updates.UpdateError):
                updates.download_update(self.release, self.cache, http)
            self.assertEqual(list(self.cache.rglob('*.zip')), [])
            self.assertEqual(list(self.cache.rglob('*.part')), [])

    def test_cancel_during_download_removes_partial(self):
        event = threading.Event()
        with self.assertRaises(updates.Cancelled):
            updates.download_update(self.release, self.cache, self.http, event,
                                    progress=lambda *_: event.set())
        self.assertEqual(list(self.cache.rglob('*.zip')), [])
        self.assertEqual(list(self.cache.rglob('*.part')), [])

    def test_no_space_does_not_download_payload(self):
        with patch('flintdock_updates.shutil.disk_usage', return_value=shutil._ntuple_diskusage(100, 100, 0)), self.assertRaises(updates.UpdateError):
            updates.download_update(self.release, self.cache, self.http)
        self.assertEqual(self.http.calls, [])

    def test_corrupted_cached_file_is_not_overwritten(self):
        target = updates.download_update(self.release, self.cache, self.http)
        target.write_bytes(b'changed file')
        with self.assertRaises(updates.UpdateError):
            updates.download_update(self.release, self.cache, self.http)
        self.assertEqual(target.read_bytes(), b'changed file')

    def test_unsafe_archive_and_server_data_are_not_kept(self):
        for name in ('../escape', 'FlintDock/worlds/private/level.dat', 'FlintDock/permissions.json',
                     'other/app.exe', 'FlintDock/_internal/../escape'):
            raw = sample_zip({name: b'bad'})
            data = release_data(raw)
            with self.assertRaises(updates.UpdateError):
                updates.download_update(updates.parse_release(data, '1.3.0'), self.cache, FakeHTTP(data, raw))
            self.assertEqual(list(self.cache.rglob('*.zip')), [])

    def test_update_preferences_persist_without_touching_server_settings(self):
        before = (self.root / 'server.properties').read_bytes()
        self.assertEqual(updates.read_settings(self.cache), updates.DEFAULTS)
        preferences = {'check_enabled': True, 'auto_download': True, 'interval_hours': 12}
        updates.save_settings(self.cache, preferences)
        self.assertEqual(updates.read_settings(self.cache), preferences)
        self.assertEqual((self.root / 'server.properties').read_bytes(), before)
        (self.cache / 'settings.json').write_text('broken')
        with self.assertRaises(updates.UpdateError):
            updates.read_settings(self.cache)

    def test_interval_persists_across_restarts_and_handles_clock_rollback(self):
        settings = dict(updates.DEFAULTS)
        self.assertFalse(updates.check_due(settings, {'last_attempt': 1000}, 1001))
        self.assertTrue(updates.check_due(settings, {'last_attempt': 1000}, 22600))
        self.assertTrue(updates.check_due(settings, {'last_attempt': 1000}, 500))
        self.assertTrue(updates.check_due(settings, {'last_attempt': float('nan')}, 1000))
        self.assertFalse(updates.check_due({**settings, 'check_enabled': False}, {}, 99999))


class ControllerTests(Isolated):
    def setUp(self):
        super().setUp()
        self.app = Mock()
        self.raw = sample_zip()
        self.http = FakeHTTP(release_data(self.raw), self.raw)
        self.controller = AppUpdateController(self.app, self.root / 'cache', background=False, http=self.http)
        self.addCleanup(self.controller.close)

    def finish(self):
        deadline = time.monotonic() + 5
        while self.controller.busy and time.monotonic() < deadline:
            self.controller.tick()
            time.sleep(.01)
        self.assertFalse(self.controller.busy)

    def test_manual_check_when_auto_off_only_notifies(self):
        self.controller.check()
        self.finish()
        self.assertIsNotNone(self.controller.candidate)
        self.assertIsNone(self.controller.ready)
        self.assertEqual(self.http.calls, [updates.API_URL])
        self.app.notify.assert_called_once()

    def test_auto_on_downloads_in_worker_without_installing(self):
        self.controller.change_settings({**updates.DEFAULTS, 'auto_download': True})
        self.finish()
        self.assertTrue(self.controller.ready.is_file())
        self.assertEqual(self.controller.ready.read_bytes(), self.raw)
        self.app.manager.shutdown.assert_not_called()
        self.app.manager.send.assert_not_called()

    def test_toggle_off_before_check_result_never_downloads(self):
        self.controller.settings['auto_download'] = True
        self.controller.busy = True
        self.controller.operation_auto = True
        self.controller.change_settings(dict(updates.DEFAULTS))
        self.controller.handle('checked', updates.parse_release(self.http.payload, '1.3.0'))
        self.assertFalse(self.controller.busy)
        self.assertIsNone(self.controller.ready)
        self.assertEqual(self.http.calls, [])

    def test_repeated_check_does_not_repeat_same_notice_or_duplicate_download(self):
        self.controller.check()
        self.assertFalse(self.controller.check())
        self.finish()
        self.controller.check()
        self.finish()
        self.app.notify.assert_called_once()

    def test_manual_download_works_with_automatic_download_off(self):
        self.controller.check()
        self.finish()
        self.assertTrue(self.controller.download())
        self.finish()
        self.assertTrue(self.controller.ready.is_file())
        self.assertFalse(self.controller.settings['auto_download'])

    def test_cached_automatic_update_does_not_repeat_notifications(self):
        self.controller.change_settings({**updates.DEFAULTS, 'auto_download': True})
        self.finish()
        calls = self.app.notify.call_count
        zip_requests = self.http.calls.count(self.controller.candidate.url)
        self.controller.check()
        self.finish()
        self.assertEqual(self.app.notify.call_count, calls)
        self.assertEqual(self.http.calls.count(self.controller.candidate.url), zip_requests)

    def test_periodic_check_respects_persisted_interval(self):
        self.controller.background = True
        self.controller._next_poll = 0
        self.controller.tick()
        self.finish()
        self.controller._next_poll = 0
        self.controller.tick()
        self.assertEqual(self.http.calls, [updates.API_URL])

    def test_closing_cancels_activity_and_never_schedules_again(self):
        self.controller.close()
        self.app._repeat.reset_mock()
        self.controller.tick()
        self.assertTrue(self.controller.cancel_event.is_set())
        self.assertFalse(self.controller.check())
        self.app._repeat.assert_not_called()


class UpdaterUiTests(unittest.TestCase):
    def test_controls_fit_and_are_separate_from_bedrock_settings(self):
        with tempfile.TemporaryDirectory(prefix='flintdock-update-ui-') as temp:
            root = Path(temp)
            fixture(root)
            app = load_launcher().Launcher(root, app_update_background=False)
            errors = []
            app.report_callback_exception = lambda *args: errors.append(args)
            try:
                controller = app.app_updates
                before = dict(app.ui)
                win = controller.show()
                self.assertIs(win, controller.show())
                self.assertFalse(win.automatic.get())
                for size in ('640x660', '720x660'):
                    win.geometry(size)
                    app.update()
                    for widget in (win.check_button, win.download_button, win.cancel_button, win.status_text, win.last_text):
                        self.assertTrue(widget.winfo_ismapped())
                        self.assertGreaterEqual(widget.winfo_height(), 20)
                        self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), win.winfo_rooty() + win.winfo_height())
                    for widget in win.winfo_children()[0].winfo_children():
                        self.assertTrue(widget.winfo_ismapped())
                        self.assertGreaterEqual(widget.winfo_height(), widget.winfo_reqheight())
                self.assertEqual(str(win.status_text['state']), 'disabled')
                controller.change_settings({**updates.DEFAULTS, 'interval_hours': 12})
                self.assertEqual(app.ui, before)
                self.assertEqual(updates.read_settings(controller.root)['interval_hours'], 12)
                self.assertEqual(errors, [])
            finally:
                app.on_close()
