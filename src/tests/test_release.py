"""First-run and packaging routing tests. No actual Minecraft executable runs."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import first_run
import app_paths


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='release-setup-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / 'New Server'
        self.archive = self.root / 'server.zip'
        self.pack({'bedrock_server.exe': 'MZ-test-not-executable',
                   'server.properties': 'level-name=Old\nonline-mode=false\n',
                   'permissions.json': '[{"xuid":"test","permission":"operator"}]'})

    def pack(self, data):
        with zipfile.ZipFile(self.archive, 'w') as archive:
            for name, value in data.items():
                archive.writestr(name, value)

    def test_clean_setup_and_identity_defaults(self):
        root = first_run.install_server_archive(self.archive, self.target, True)
        self.assertEqual(root, self.target)
        self.assertEqual(json.loads((root / 'permissions.json').read_text()), [])
        self.assertEqual(json.loads((root / 'allowlist.json').read_text()), [])
        body = (root / 'server.properties').read_text()
        for line in ('level-name=My World', 'allow-list=true', 'online-mode=true', 'allow-cheats=false'):
            self.assertIn(line, body)
        self.assertFalse((root / 'worlds').exists())

    def test_requires_eula_acceptance(self):
        with self.assertRaisesRegex(ValueError, 'EULA'):
            first_run.install_server_archive(self.archive, self.target)
        self.assertFalse(self.target.exists())

    def test_existing_data_is_never_overwritten(self):
        self.target.mkdir()
        (self.target / 'precious-world').write_text('keep')
        with self.assertRaisesRegex(ValueError, 'empty folder'):
            first_run.install_server_archive(self.archive, self.target, True)
        self.assertEqual((self.target / 'precious-world').read_text(), 'keep')

    def test_traversal_rejected_before_writing(self):
        self.pack({'bedrock_server.exe': 'x', 'server.properties': '', '../escape': 'x'})
        with self.assertRaises(ValueError):
            first_run.install_server_archive(self.archive, self.target, True)
        self.assertFalse(self.target.exists())
        self.assertFalse((self.root / 'escape').exists())

    def test_existing_world_archive_rejected(self):
        self.pack({'bedrock_server.exe': 'x', 'server.properties': '', 'worlds/private/db/CURRENT': 'x'})
        with self.assertRaisesRegex(ValueError, 'existing server data'):
            first_run.install_server_archive(self.archive, self.target, True)

    def test_wrong_archive_rejected(self):
        self.pack({'bedrock_server': 'Linux'})
        with self.assertRaisesRegex(ValueError, 'Windows'):
            first_run.install_server_archive(self.archive, self.target, True)

    def test_empty_existing_directory_accepted(self):
        self.target.mkdir()
        first_run.install_server_archive(self.archive, self.target, True)
        first_run.validate_server(self.target)

    def test_connect_does_not_write_existing_files(self):
        first_run.install_server_archive(self.archive, self.target, True)
        before = {p.name: p.read_bytes() for p in self.target.iterdir()}
        first_run.validate_server(self.target)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.target.iterdir()})

    def test_application_folder_is_never_a_data_folder(self):
        for path in (app_paths.APP_ROOT, app_paths.APP_ROOT / 'worlds', app_paths.APP_ROOT.parent):
            with self.assertRaises(ValueError):
                first_run.data_location(path)

    def test_helper_whitelist_and_explicit_server_path(self):
        with self.assertRaises(ValueError):
            app_paths.worker_command('arbitrary_script')
        cmd = app_paths.worker_command('bedrock_addons.py', 'list', '--server', self.target)
        self.assertEqual(cmd[-2:], ['--server', str(self.target)])
        self.assertNotEqual(Path(cmd[2]).parent, self.target)
