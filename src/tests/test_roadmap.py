from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import urllib.request
import urllib.error
import zipfile

from test_tooling import Isolated, ROOT
import bedrock_recovery as recovery
import bedrock_mods as mods
import bedrock_experience as experience
from bedrock_runtime import rpc, ManagerClient
from bedrock_storage import atomic_json, atomic_text, operation_lock
import bedrock_addons as addons
from bedrock_rehearsal import rehearse

TESTS = Path(__file__).parent


def until(predicate, seconds=10):
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (OSError, ValueError, RuntimeError):
            pass
        time.sleep(.05)
    raise AssertionError('Timed out waiting for the expected process state')


class RecoveryTests(Isolated):
    def test_process_termination_recovery_restores_prior_files(self):
        for name, value in [('one.txt', 'one'), ('two.txt', 'two'), ('new.txt', 'new')]:
            atomic_text(self.root / name, value)
        proc = subprocess.Popen([sys.executable, str(TESTS / 'interrupted_operation.py'), str(self.root)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            until(lambda: (self.root / 'ready-to-kill').exists())
            self.assertEqual((self.root / 'one.txt').read_text(), 'new')
            proc.terminate()
            proc.wait(timeout=10)
            pending = recovery.operations(self.root, True)
            self.assertEqual(len(pending), 1)
            with self.assertRaises(RuntimeError):
                recovery.assert_recovered(self.root)
            with operation_lock(self.root):
                recovery.recover(self.root, pending[0]['id'])
            self.assertEqual((self.root / 'one.txt').read_text(), 'one')
            self.assertEqual((self.root / 'two.txt').read_text(), 'two')
            self.assertEqual(recovery.operations(self.root, True), [])
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stderr.close()

    def test_complete_point_restores_settings_packs_world_and_binary(self):
        atomic_text(self.root / 'bedrock_server.exe', 'old-binary')
        atomic_json(self.root / 'bedrock_version.json', {'version': '1.0.0'})
        pack = self.root / 'behavior_packs' / 'custom'
        pack.mkdir(parents=True)
        atomic_text(pack / 'data.txt', 'old-pack')
        point = recovery.create_restore_point(self.root, 'Known good')
        self.assertEqual(recovery.verify_restore_point(point)['label'], 'Known good')
        atomic_text(self.root / 'bedrock_server.exe', 'new-binary')
        atomic_text(pack / 'data.txt', 'new-pack')
        atomic_text(self.world / 'level.dat', 'new-world')
        safety = recovery.restore_point(self.root, point)
        self.assertTrue(safety.exists())
        self.assertEqual((self.root / 'bedrock_server.exe').read_text(), 'old-binary')
        self.assertEqual((pack / 'data.txt').read_text(), 'old-pack')
        self.assertEqual((self.world / 'level.dat').read_bytes(), b'test-level')

    def test_prepare_failure_does_not_change_live_files(self):
        target = self.root / 'one.txt'
        atomic_text(target, 'original')
        transaction = recovery.Transaction(self.root, 'Test')
        with self.assertRaises(FileNotFoundError):
            transaction.replace(target, self.root / 'missing')
        transaction.cancel()
        self.assertEqual(target.read_text(), 'original')
        self.assertEqual(recovery.operations(self.root, True), [])

    def test_restore_point_requires_world_in_its_restore_plan(self):
        path = self.zip({'restore-point.json': json.dumps({'world': self.world.name,
            'paths': ['server.properties']}), 'server.properties': 'level-name=Bedrock level',
            'worlds/Bedrock level/level.dat': 'data', 'worlds/Bedrock level/db/CURRENT': 'data'})
        with self.assertRaises(ValueError):
            recovery.verify_restore_point(path)

    def test_cancel_during_update_preparation_preserves_live_files(self):
        from bedrock_update import apply_update, UpdateError
        atomic_text(self.root / 'bedrock_server.exe', 'original')
        archive = self.zip({'bedrock_server.exe': 'replacement'})
        def cancel(done, total, label):
            if label.startswith('Preparing recovery'):
                raise RuntimeError('Cancelled by test')
        with patch('bedrock_update.server_running', return_value=False), self.assertRaises(UpdateError):
            apply_update(self.root, archive, '1.0.1', progress=cancel, backup=False)
        self.assertEqual((self.root / 'bedrock_server.exe').read_text(), 'original')
        self.assertEqual(recovery.operations(self.root, True), [])


class ExperienceTests(Isolated):
    def test_settings_preview_save_and_undo(self):
        path = self.root / 'server.properties'
        original = path.read_text()
        updated, changes = experience.settings_preview(original, {'max-players': '20'})
        self.assertEqual(changes, [('max-players', '10', '20')])
        experience.save_settings(self.root, original, updated)
        self.assertEqual(experience.properties(path.read_text())['max-players'], '20')
        experience.undo_settings(self.root)
        self.assertEqual(path.read_text(), original)

    def test_external_settings_edit_is_not_overwritten(self):
        path = self.root / 'server.properties'
        before = path.read_text()
        atomic_text(path, before + 'allow-list=true\n')
        with self.assertRaises(RuntimeError):
            experience.save_settings(self.root, before, before + 'allow-list=false\n')
        self.assertTrue(path.read_text().endswith('allow-list=true\n'))

    def test_backup_metadata_invalidates_verification_after_change(self):
        from bedrock_storage import create_backup
        path = create_backup(self.root)
        experience.label_backup(self.root, path, 'Before the castle')
        experience.verify_catalogued(self.root, path)
        record = experience.backup_catalogue(self.root)[0]
        self.assertEqual(record['label'], 'Before the castle')
        self.assertTrue(record['verified'])
        with path.open('ab') as stream:
            stream.write(b'changed')
        self.assertEqual(experience.backup_catalogue(self.root)[0]['verified'], '')

    def test_incident_redacts_identifiers_and_groups_errors(self):
        atomic_text(self.root / 'server_manager.log', '[ERROR] dependency failed\n[ERROR] dependency failed\nxuid: 123456 192.168.1.4')
        report = experience.incident_report(self.root)
        self.assertIn('2x dependency failed', report)
        self.assertNotIn('123456', report)
        self.assertNotIn('192.168.1.4', report)
        self.assertEqual(experience.redact('Version: 1.26.45.1'), 'Version: 1.26.45.1')


class ModTests(Isolated):
    UUID = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
    def pack(self, folder='incoming', version=None, dependencies=None):
        path = self.root / folder
        path.mkdir(parents=True, exist_ok=True)
        manifest = {'header': {'uuid': self.UUID, 'name': 'Example', 'version': version or [1, 0, 0]},
                    'modules': [{'type': 'data'}], 'dependencies': dependencies or []}
        atomic_json(path / 'manifest.json', manifest)
        atomic_text(path / 'content.txt', str(version or [1, 0, 0]))
        return addons.discover_packs(path)[0]

    def test_addon_failure_restores_pack_and_registration(self):
        server = addons.Server(self.root)
        state = {}
        mods.install_packs(server, [self.pack()], None, state, 'example.mcpack', 'a')
        original = state['example.mcpack']
        target = self.root / 'behavior_packs' / original['packs'][0]['folder'] / 'content.txt'
        real = os.replace
        def fail_registration(src, dest):
            if Path(dest).name == 'world_behavior_packs.json' and '.operations' in str(src):
                raise PermissionError('Injected registration failure')
            return real(src, dest)
        with patch.object(os, 'replace', side_effect=fail_registration), self.assertRaises(PermissionError):
            mods.install_packs(server, [self.pack(version=[2, 0, 0])], original, state, 'example.mcpack', 'b')
        self.assertEqual(target.read_text(), '[1, 0, 0]')
        self.assertEqual(mods.active_packs(self.root)['behavior'][0]['version'], [1, 0, 0])
        self.assertEqual(server.load_state()['example.mcpack']['sha256'], 'a')

    def test_missing_dependency_blocks_install_before_changes(self):
        server = addons.Server(self.root)
        pack = self.pack(dependencies=[{'uuid': 'ffffffff-bbbb-cccc-dddd-eeeeeeeeeeee', 'version': [1,0,0]}])
        with self.assertRaises(ValueError):
            mods.install_packs(server, [pack], None, {}, 'example.mcpack', 'a')
        self.assertEqual(mods.active_packs(self.root)['behavior'], [])

    def test_bundled_vanilla_layers_are_not_duplicate_custom_packs(self):
        for folder in ('vanilla', 'vanilla_1.21.0', 'vanilla_1.20.0'):
            atomic_json(self.root / 'behavior_packs' / folder / 'manifest.json', {
                'header': {'uuid': mods.VANILLA_UUIDS['behavior'], 'name': 'Vanilla', 'version': [0, 0, 1]}})
        index, duplicates = mods.pack_index(self.root)
        self.assertEqual(duplicates, [])
        self.assertEqual(index[mods.VANILLA_UUIDS['behavior']]['folder'], 'vanilla')

    def test_profile_comparison_and_apply(self):
        server = addons.Server(self.root)
        state = {}
        mods.install_packs(server, [self.pack()], None, state, 'example.mcpack', 'a')
        mods.save_profile(self.root, 'Working')
        atomic_json(self.world / 'world_behavior_packs.json', [])
        changes = mods.compare_profile(self.root, 'Working')
        self.assertEqual(changes['enable'], [self.UUID])
        self.assertEqual(changes['issues'], [])
        mods.apply_profile(self.root, 'Working')
        self.assertEqual(mods.active_packs(self.root)['behavior'][0]['pack_id'], self.UUID)


class RuntimeTests(Isolated):
    def test_real_process_reconnect_commands_history_and_shutdown(self):
        from player_history import PlayerHistory
        history = PlayerHistory(self.root)
        history.queue_add('Alex', 'say Queued while detached')
        history.close()
        log = (self.root / 'runtime-test.log').open('wb')
        proc = subprocess.Popen([sys.executable, '-u', str(TESTS / 'runtime_driver.py'), str(self.root)],
                                stdout=log, stderr=log)
        client = ManagerClient(self.root, queue.Queue())
        try:
            until(lambda: rpc(self.root, 'status')['state']['server_up'])
            descriptor = json.loads((self.root / '.manager-runtime.json').read_text())
            request = urllib.request.Request(f"http://127.0.0.1:{descriptor['port']}/rpc",
                data=b'{"method":"status"}', headers={'Authorization': 'Bearer incorrect'})
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=2)
            self.assertEqual(denied.exception.code, 403)
            denied.exception.close()
            client.attach()
            until(client.running)
            self.assertTrue(client.send('test-join'))
            until(lambda: 'Alex' in rpc(self.root, 'status')['state']['players'])
            # Commands created after this join must wait for a subsequent join.
            from player_permissions import permissions_snapshot
            history = PlayerHistory(self.root)
            history.queue_add('Alex', 'say Next join only')
            history.close()
            result = rpc(self.root, 'player_role', xuid='123', role='operator',
                         revision=permissions_snapshot(self.root)['revision'])
            self.assertTrue(result['reload_sent'])
            self.assertEqual(permissions_snapshot(self.root)['rows'][0]['permission'], 'operator')
            client.disconnect()
            self.assertIsNone(proc.poll(), 'Closing the client must keep its manager alive')
            until(lambda: 'Queued while detached' in (self.root / 'server_manager.log').read_text(), seconds=12)
            self.assertNotIn('Next join only', (self.root / 'server_manager.log').read_text())
            other = ManagerClient(self.root, queue.Queue())
            other.attach()
            until(other.running)
            self.assertIn('Alex', other.state['players'])
            self.assertTrue(other.send('test-leave'))
            until(lambda: not rpc(self.root, 'status')['state']['players'])
            result = rpc(self.root, 'player_role', xuid='123', role='visitor',
                         revision=permissions_snapshot(self.root)['revision'])
            self.assertTrue(result['reload_sent'])
            self.assertEqual(permissions_snapshot(self.root)['rows'][0]['permission'], 'visitor')
            self.assertTrue(other.send('test-join'))
            until(lambda: 'Next join only' in (self.root / 'server_manager.log').read_text(), seconds=12)
            self.assertTrue(other.send('!quit'))
            proc.wait(timeout=10)
            other.disconnect()
            self.assertEqual(proc.returncode, 0)
            history = PlayerHistory(self.root)
            try:
                self.assertEqual(history.queue_for('Alex'), [])
                self.assertEqual(history.players()[0]['online'], 0)
                self.assertGreaterEqual(history.players()[0]['sessions'], 1)
            finally:
                history.close()
        finally:
            client.disconnect()
            if proc.poll() is None:
                try:
                    rpc(self.root, 'send', command='!quit')
                    proc.wait(timeout=10)
                except Exception:
                    proc.kill()
                    proc.wait()
            log.close()

    def test_crash_state_cleanup_closes_sessions(self):
        from server_manager import Manager
        from player_history import PlayerHistory
        manager = Manager(self.root, SimpleNamespace(daemon=True, now=False, server_command=['unused']))
        manager.history = PlayerHistory(self.root)
        try:
            manager.on_output('[2026-09-04 12:00:00 INFO] Player connected: Alex, xuid: 123456')
            self.assertIn('Alex', manager.players)
            manager.reset_live_state()
            self.assertEqual(manager.players, set())
            self.assertEqual(manager._queue_due, {})
            self.assertEqual(manager.history.players()[0]['online'], 0)
        finally:
            manager.history.close()

    def test_maintenance_maximum_delay_warns_and_empty_server_does_not_wait(self):
        from server_manager import Manager
        manager = Manager(self.root, SimpleNamespace(daemon=True, now=False, server_command=['unused']))
        atomic_json(manager.config_path, {'maintenance_wait_empty': True, 'maintenance_max_delay': 0})
        with patch.object(manager.server, 'announce') as announce, patch.object(manager.stopping, 'wait', return_value=False) as wait:
            self.assertTrue(manager.wait_for_players('Test maintenance'))
            wait.assert_not_called()
            manager.players.add('Alex')
            self.assertTrue(manager.wait_for_players('Test maintenance'))
            wait.assert_called_once_with(60)
            self.assertIn('one minute', announce.call_args.args[0])

    def test_runtime_logs_rotate(self):
        from bedrock_runtime import Runtime
        runtime = Runtime(SimpleNamespace(root=self.root))
        runtime.start()
        try:
            runtime.handler.maxBytes = 256
            for i in range(20):
                runtime.emit(f'{i}: ' + 'x'*100)
            self.assertTrue((self.root / 'server_manager.log.1').exists())
            self.assertLessEqual(len(list(self.root.glob('server_manager.log*'))), 6)
        finally:
            runtime.close()

    def test_rehearsal_uses_copy_and_clean_stop(self):
        archive = self.zip({'bedrock_server.exe': b'fake-server'})
        before = (self.world / 'level.dat').read_bytes()
        with patch('bedrock_update.server_running', return_value=False):
            report, path = rehearse(self.root, archive, '1.0.1', timeout=8, settle=.15,
                command=[sys.executable, '-u', str(TESTS / 'fake_bds.py')])
        self.assertTrue(report['passed'], report)
        self.assertTrue(path.exists())
        self.assertNotIn(19132, report['ports'])
        self.assertEqual((self.world / 'level.dat').read_bytes(), before)
        self.assertEqual(list(self.root.glob('.rehearsal-*')), [])


if __name__ == '__main__':
    unittest.main()
