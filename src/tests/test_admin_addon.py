"""Build/install tests only use disposable files; never touch the installed addon."""
import hashlib
import json
import os
from pathlib import Path
import shutil
from unittest.mock import patch
import zipfile

from test_tooling import Isolated
import build_admin_addon as builder
import bedrock_addons as addons
import bedrock_recovery as recovery
from bedrock_storage import atomic_json, atomic_text


class AdminAddonTests(Isolated):
    def install(self):
        with patch.object(builder, 'server_running', return_value=False):
            return builder.install(self.root)

    def test_package_has_only_runtime_assets(self):
        target = builder.build_package(builder.SOURCE, self.root / 'output.mcaddon')
        with zipfile.ZipFile(target) as archive:
            self.assertEqual(set(archive.namelist()), {'RestartManagerLink/' + name for name in
                builder.ASSETS})
            self.assertEqual(json.loads(archive.read('RestartManagerLink/manifest.json'))['header']['version'], [1, 9, 0])

    def test_install_keeps_other_addons_and_matches_package_state(self):
        server = addons.Server(self.root)
        unrelated = {'disabled': True, 'packs': [], 'sha256': 'untouched'}
        atomic_json(server.state_path, {'other.mcaddon': unrelated})
        path = self.install()
        state = server.load_state()
        self.assertEqual(state['other.mcaddon'], unrelated)
        self.assertEqual(state[builder.PACKAGE]['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(json.loads((self.world / 'world_behavior_packs.json').read_text())[0]['version'], [1, 9, 0])
        self.assertEqual(recovery.operations(self.root, True), [])
        self.assertTrue(list((self.root / 'backups').glob('*.zip')))

    def test_upgrade_preserves_server_generated_catalog(self):
        self.install()
        catalog = self.root / 'behavior_packs/Restart_Manager_Link__7b3c1e42/scripts/catalog.js'
        atomic_text(catalog, 'export const CATALOG = {packs: [], generated: "server-specific"};')
        path = self.install()
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(archive.read('RestartManagerLink/scripts/catalog.js'), catalog.read_bytes())

    def test_archive_failure_rolls_back_files_registration_state_and_archive(self):
        path = self.install()
        before_package = path.read_bytes()
        server = addons.Server(self.root)
        before_state = server.state_path.read_bytes()
        registration = self.world / 'world_behavior_packs.json'
        before_registration = registration.read_bytes()
        installed = self.root / 'behavior_packs/Restart_Manager_Link__7b3c1e42/scripts/admin.js'
        before_script = installed.read_bytes()
        source = self.root / 'incoming'
        shutil.copytree(builder.SOURCE, source)
        atomic_text(source / 'scripts/admin.js', '// replacement content')
        original = os.replace
        def fail(src, dst):
            if Path(dst) == path and '.operations' in str(src):
                raise PermissionError('Injected archive replacement failure')
            return original(src, dst)
        with patch.object(os, 'replace', side_effect=fail), patch.object(builder, 'server_running', return_value=False):
            with self.assertRaises(PermissionError):
                builder.install(self.root, source)
        self.assertEqual(path.read_bytes(), before_package)
        self.assertEqual(installed.read_bytes(), before_script)
        self.assertEqual(server.state_path.read_bytes(), before_state)
        self.assertEqual(registration.read_bytes(), before_registration)
        self.assertEqual(recovery.operations(self.root, True), [])

    def test_running_server_disabled_addon_and_pending_operation_block_install(self):
        with patch.object(builder, 'server_running', return_value=True), self.assertRaises(RuntimeError):
            builder.install(self.root)
        server = addons.Server(self.root)
        atomic_json(server.state_path, {builder.PACKAGE: {'disabled': True}})
        with self.assertRaises(RuntimeError):
            self.install()
        atomic_json(server.state_path, {})
        transaction = recovery.Transaction(self.root, 'Unfinished test')
        try:
            with self.assertRaises(RuntimeError):
                self.install()
        finally:
            transaction.cancel()

    def test_identical_bytes_build_identical_packages_despite_timestamps(self):
        source = self.root / 'source'
        shutil.copytree(builder.SOURCE, source)
        first = builder.build_package(source, self.root / 'one.mcaddon').read_bytes()
        for path in source.rglob('*.js'):
            os.utime(path, (1700000000, 1700000000))
        second = builder.build_package(source, self.root / 'two.mcaddon').read_bytes()
        self.assertEqual(first, second)

    def test_source_only_files_are_never_installed(self):
        source = self.root / 'source'
        shutil.copytree(builder.SOURCE, source)
        atomic_text(source / 'scripts/qa.js', 'throw new Error("Never install this test");')
        atomic_text(source / 'development-notes.txt', 'Private source-only notes')
        with patch.object(builder, 'server_running', return_value=False):
            builder.install(self.root, source)
        target = self.root / 'behavior_packs/Restart_Manager_Link__7b3c1e42'
        self.assertEqual({p.relative_to(target).as_posix() for p in target.rglob('*') if p.is_file()}, set(builder.ASSETS))

    def test_invalid_identity_fails_without_overwriting_existing_package(self):
        source = self.root / 'source'
        shutil.copytree(builder.SOURCE, source)
        manifest_path = source / 'manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['header']['uuid'] = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
        atomic_json(manifest_path, manifest)
        output = self.root / 'previous.mcaddon'
        atomic_text(output, 'keep previous package')
        with self.assertRaises(ValueError):
            builder.build_package(source, output)
        self.assertEqual(output.read_text(), 'keep previous package')
