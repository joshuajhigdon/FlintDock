"""Regression tests use disposable worlds; never the installed server or player DB."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import bedrock_storage as storage
import bedrock_update as updater
import bedrock_addons as addons
import launcher_health as health
import server_manager as manager
import player_history


def load_launcher():
    loader = importlib.machinery.SourceFileLoader('bedrock_launcher_test', str(ROOT / 'BedrockLauncher.pyw'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def fixture(root):
    storage.atomic_text(root / 'server.properties', 'server-name=Workshop\nlevel-name=Bedrock level\nmax-players=10\ntick-distance=4\n')
    world = root / 'worlds' / 'Bedrock level'
    (world / 'db').mkdir(parents=True)
    (world / 'level.dat').write_bytes(b'test-level')
    (world / 'db' / 'CURRENT').write_bytes(b'test-current')
    storage.atomic_json(root / 'launcher_ui.json', {'update_check': 'off', 'page': 'dashboard'})
    return world


class Isolated(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bedrock-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.world = fixture(self.root)

    def zip(self, entries, name='input.zip'):
        path = self.root / name
        with zipfile.ZipFile(path, 'w') as archive:
            for key, value in entries.items():
                archive.writestr(key, value)
        return path


class StorageTests(Isolated):
    def test_backup_roundtrip_and_recovery_copy(self):
        backup = storage.create_backup(self.root)
        self.assertEqual(storage.verify_backup(backup)['files'], 2)
        (self.world / 'db' / 'CURRENT').write_bytes(b'new-progress')
        safety = storage.restore_backup(self.root, backup)
        self.assertEqual((self.world / 'db' / 'CURRENT').read_bytes(), b'test-current')
        with zipfile.ZipFile(safety) as archive:
            self.assertEqual(archive.read('db/CURRENT'), b'new-progress')

    def test_invalid_restore_keeps_current_world(self):
        bad = self.zip({'notes.txt': 'not a world'})
        with self.assertRaises(ValueError):
            storage.restore_backup(self.root, bad)
        self.assertEqual((self.world / 'level.dat').read_bytes(), b'test-level')

    def test_unsafe_archive_paths_rejected_before_extraction(self):
        for unsafe in ('../escape', '../outside-sibling/a', '/absolute', 'C:/drive',
                       'db/../../escape', 'file:stream', 'db/CON.txt', 'db/file.',
                       '..\\escape', 'db//file'):
            with self.subTest(path=unsafe):
                path = self.zip({'safe': 'data', unsafe: 'bad'})
                with zipfile.ZipFile(path) as archive, self.assertRaises(ValueError):
                    storage.extract_zip(archive, self.root / 'extract')
                self.assertFalse((self.root / 'extract' / 'safe').exists())

    def test_case_aliases_and_links_rejected(self):
        path = self.zip({'level.dat': 'a', 'LEVEL.DAT': 'b'})
        with zipfile.ZipFile(path) as archive, self.assertRaises(ValueError):
            storage.zip_members(archive, self.root / 'dest')
        with zipfile.ZipFile(path, 'w') as archive:
            member = zipfile.ZipInfo('link')
            member.external_attr = 0o120777 << 16
            archive.writestr(member, '../elsewhere')
        with zipfile.ZipFile(path) as archive, self.assertRaises(ValueError):
            storage.zip_members(archive, self.root / 'dest')

    def test_invalid_world_name(self):
        for name in ('../elsewhere', '', '.', 'C:\\world', 'nested/world'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                storage.world_path(self.root, name)

    def test_retention_protects_manual_legacy_and_other_worlds(self):
        manual = storage.create_backup(self.root)
        legacy = self.zip({'level.dat': 'test', 'db/CURRENT': 'test'}, 'legacy.zip')
        auto = [storage.create_backup(self.root, 'auto') for _ in range(3)]
        removed = storage.prune_automatic(self.root, keep=1)
        self.assertEqual(len(removed), 2)
        self.assertTrue(auto[-1].exists())
        self.assertTrue(manual.exists())
        self.assertTrue(legacy.exists())

    def test_failed_atomic_write_preserves_original(self):
        path = self.root / 'config.json'
        storage.atomic_json(path, {'old': True})
        with patch.object(storage.os, 'replace', side_effect=PermissionError('locked')):
            with self.assertRaises(PermissionError):
                storage.atomic_json(path, {'new': True})
        self.assertEqual(json.loads(path.read_text()), {'old': True})
        self.assertEqual(list(self.root.glob('.config.json.*.tmp')), [])

    def test_operation_lock_released_and_prevents_overlap(self):
        with storage.operation_lock(self.root):
            with self.assertRaises(RuntimeError), storage.operation_lock(self.root):
                pass
        with storage.operation_lock(self.root):
            pass


class HistoryTests(Isolated):
    def setUp(self):
        super().setUp()
        self.h = player_history.PlayerHistory(self.root)
        self.addCleanup(self.h.close)

    def test_duplicate_console_and_addon_events_count_one_session(self):
        self.h.ingest_line('[2026-09-04 10:00:00 INFO] Player connected: Steve, xuid: 123')
        self.h.ingest_line('[2026-09-04 10:00:02 WARN] [MGR]|ev|{"p":"Steve","k":"join"}')
        self.h.ingest_line('[2026-09-04 10:01:00 INFO] Player disconnected: Steve, xuid: 123')
        self.h.ingest_line('[2026-09-04 10:01:02 WARN] [MGR]|ev|{"p":"Steve","k":"leave"}')
        player = self.h.players()[0]
        self.assertEqual((player['sessions'], player['seconds']), (1, 60))
        self.assertEqual(len(self.h.timeline()), 2)

    def test_failed_command_delivery_stays_pending(self):
        self.h.queue_add('Alex', 'give @s diamond')
        self.assertEqual(self.h.queue_deliver('Alex', lambda cmd: False), [])
        self.assertEqual(self.h.stats()['queued'], 1)
        commands = []
        self.h.queue_deliver('Alex', lambda cmd: commands.append(cmd) or True)
        self.assertEqual(commands, ['give "Alex" diamond'])
        self.assertEqual(self.h.stats()['queued'], 0)

    def test_shutdown_retains_playtime(self):
        self.h.player_joined('Alex', ts='2026-09-04 12:00:00')
        self.h.end_sessions('2026-09-04 12:02:00')
        self.assertEqual(self.h.players()[0]['seconds'], 120)
        self.assertEqual(self.h.players()[0]['online'], 0)

    def test_locked_database_is_not_quarantined(self):
        self.h.close()
        with patch.object(player_history.PlayerHistory, '_create_schema',
                          side_effect=sqlite3.OperationalError('database is locked')):
            with self.assertRaises(sqlite3.OperationalError):
                player_history.PlayerHistory(self.root)
        self.assertEqual(list(self.root.glob('*.corrupt-*')), [])


class UpdateTests(Isolated):
    def setUp(self):
        super().setUp()
        self.running = patch.object(updater, 'server_running', return_value=False)
        self.running.start()
        self.addCleanup(self.running.stop)
        (self.root / 'bedrock_server.exe').write_bytes(b'old-server')
        updater.write_marker(self.root, '1.0.0')

    def test_update_preserves_settings_world_and_custom_packs(self):
        original = (self.root / 'server.properties').read_bytes()
        path = self.zip({'bedrock_server.exe': b'new-server', 'SERVER.PROPERTIES': 'defaults',
                         'worlds/Bedrock level/level.dat': 'new-world', 'data/a.json': '{}'})
        report = updater.apply_update(self.root, path, '1.0.1')
        self.assertEqual(report['written'], 2)
        self.assertEqual((self.root / 'server.properties').read_bytes(), original)
        self.assertEqual((self.world / 'level.dat').read_bytes(), b'test-level')
        self.assertEqual(updater.installed_version(self.root), '1.0.1')
        self.assertTrue(Path(report['backup']).exists())

    def test_mid_update_failure_restores_binary_and_version(self):
        path = self.zip({'bedrock_server.exe': b'new-server', 'z-last.txt': 'fail'})
        real_replace = os.replace
        def fail_last(source, dest):
            if Path(dest).name == 'z-last.txt':
                raise PermissionError('injected locked file')
            return real_replace(source, dest)
        with patch.object(updater.os, 'replace', side_effect=fail_last):
            with self.assertRaises(updater.UpdateError):
                updater.apply_update(self.root, path, '1.0.1')
        self.assertEqual((self.root / 'bedrock_server.exe').read_bytes(), b'old-server')
        self.assertEqual(updater.installed_version(self.root), '1.0.0')
        self.assertFalse((self.root / 'z-last.txt').exists())

    def test_traversal_rejected_without_replacing_binary(self):
        path = self.zip({'bedrock_server.exe': b'new-server', '../escape': 'bad'})
        with self.assertRaises(ValueError):
            updater.apply_update(self.root, path, '1.0.1')
        self.assertEqual((self.root / 'bedrock_server.exe').read_bytes(), b'old-server')


class ConfigurationTests(Isolated):
    def test_addon_global_flags_work_before_or_after_subcommand(self):
        from unittest.mock import MagicMock
        for arguments in (['--server', str(self.root), '--dry-run', 'install'],
                          ['install', '--server', str(self.root), '--dry-run']):
            with self.subTest(arguments=arguments), patch.object(sys, 'argv', ['bedrock_addons.py'] + arguments), \
                    patch.object(addons, 'cmd_install', return_value=0) as install:
                self.assertEqual(addons.main(), 0)
                server, args = install.call_args.args
                self.assertEqual(server.root, self.root)
                self.assertTrue(args.dry_run)

    def test_invalid_restart_config_recovers(self):
        path = self.root / 'manager_config.json'
        storage.atomic_json(path, {'restart_times': ['99:99'], 'backup_before_restart': 'false',
                                   'warn_minutes': [-2, 0, 5], 'backup_keep': None})
        data = manager.load_config(path)
        self.assertEqual(data['restart_times'], manager.RESTART_TIMES)
        self.assertEqual(data['warn_minutes'], [5])
        self.assertFalse(data['backup_before_restart'])
        self.assertEqual(data['backup_keep'], 10)

    def test_invalid_properties_reported(self):
        errors = health.validate_properties({'tick-distance': '99', 'server-port': '-1',
                                              'online-mode': 'maybe'})
        self.assertEqual(len(errors), 3)

    def test_nested_addon_failure_is_not_silently_ignored(self):
        path = self.zip({'nested.mcpack': b'broken'})
        with self.assertRaises(addons.Abort):
            addons.extract_archive(path, self.root / 'unpack')

    def test_manager_countdown_detects_crash(self):
        from types import SimpleNamespace
        instance = object.__new__(manager.Manager)
        import threading
        instance.stopping = threading.Event()
        instance.server = SimpleNamespace(alive=lambda: False)
        instance.countdown(manager.now() + manager.timedelta(hours=1))


if __name__ == '__main__':
    unittest.main()
