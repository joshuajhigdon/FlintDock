"""Player-directory regressions. All identities and server files are synthetic."""
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_tooling import Isolated, fixture, load_launcher
from bedrock_storage import atomic_json
from player_history import PlayerHistory, render_queue_command
from player_permissions import directory_snapshot, permissions_snapshot, set_player_role
from bedrock_runtime import Runtime


class DirectoryDataTests(Isolated):
    def setUp(self):
        super().setUp()
        self.h = PlayerHistory(self.root)
        self.addCleanup(self.h.close)
        self.h.player_joined('Offline Player', '123', '2026-01-01 10:00:00')
        self.h.player_left('Offline Player', '2026-01-01 10:02:00')

    def test_union_keeps_offline_history_allowlist_and_live_players(self):
        atomic_json(self.root / 'allowlist.json', [{'name': 'New Player', 'xuid': '456'}])
        self.h.queue_add('Offline Player', 'give @s bread 3')
        data = directory_snapshot(self.root, self.h, {'Online Player'})
        self.assertEqual([p['name'] for p in data['players']][0], 'Online Player')
        people = {p['name']: p for p in data['players']}
        self.assertEqual(set(people), {'Offline Player', 'New Player', 'Online Player'})
        self.assertEqual(people['Offline Player']['queued'], 1)
        self.assertEqual(people['Offline Player']['playtime'], '2m')
        self.assertFalse(people['Offline Player']['online'])
        self.assertEqual(people['New Player']['xuid'], '456')

    def test_live_set_overrides_stale_database_online_flags(self):
        self.h.player_joined('Stale', '444', '2026-01-01 10:00:00')
        person = next(p for p in directory_snapshot(self.root, self.h, set())['players'] if p['name'] == 'Stale')
        self.assertFalse(person['online'])
        self.assertEqual(person['playtime'], '0s')

    def test_role_writes_only_target_preserves_unknown_fields_and_backup(self):
        rows = [{'xuid': '123', 'permission': 'member', 'custom': 'keep'},
                {'xuid': '456', 'permission': 'operator', 'note': 'untouched'}]
        atomic_json(self.root / 'permissions.json', rows)
        old = (self.root / 'permissions.json').read_bytes()
        result = set_player_role(self.root, '123', 'visitor', permissions_snapshot(self.root)['revision'])
        saved = json.loads((self.root / 'permissions.json').read_text())
        self.assertEqual(saved[0], {'xuid': '123', 'permission': 'visitor', 'custom': 'keep'})
        self.assertEqual(saved[1], rows[1])
        self.assertEqual((self.root / 'permission-history' / result['backup']).read_bytes(), old)
        self.assertEqual((self.world / 'level.dat').read_bytes(), b'test-level')

    def test_each_supported_role_persists_without_changing_default(self):
        before = (self.root / 'server.properties').read_bytes()
        for role in ('operator', 'member', 'visitor'):
            set_player_role(self.root, '123', role, permissions_snapshot(self.root)['revision'])
            person = directory_snapshot(self.root, self.h, set())['players'][0]
            self.assertEqual(person['role'], role)
            self.assertEqual(person['role_source'], 'Saved override')
        self.assertEqual((self.root / 'server.properties').read_bytes(), before)

    def test_missing_identity_and_invalid_roles_never_write(self):
        for xuid, role in (('', 'operator'), ('abc', 'member'), ('0', 'visitor'), ('123', 'owner')):
            with self.assertRaises(ValueError):
                set_player_role(self.root, xuid, role, 'missing')
        self.assertFalse((self.root / 'permissions.json').exists())

    def test_malformed_or_duplicate_permissions_never_overwritten(self):
        for content in ('broken', '{}', '[{"xuid":"123","permission":"owner"}]',
                        '[{"xuid":"123","permission":"member"},{"xuid":"123","permission":"operator"}]'):
            (self.root / 'permissions.json').write_text(content)
            with self.assertRaises(ValueError):
                set_player_role(self.root, '123', 'operator', 'missing')
            self.assertEqual((self.root / 'permissions.json').read_text(), content)
            snapshot = directory_snapshot(self.root, self.h, set())
            self.assertIsNone(snapshot['revision'])
            self.assertEqual(snapshot['players'][0]['role'], 'unknown')

    def test_external_edit_invalidates_role_preview(self):
        revision = permissions_snapshot(self.root)['revision']
        atomic_json(self.root / 'permissions.json', [{'xuid': '999', 'permission': 'member'}])
        before = (self.root / 'permissions.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'changed'):
            set_player_role(self.root, '123', 'operator', revision)
        self.assertEqual((self.root / 'permissions.json').read_bytes(), before)

    def test_failed_save_preserves_existing_permissions(self):
        atomic_json(self.root / 'permissions.json', [{'xuid': '123', 'permission': 'member'}])
        snapshot = permissions_snapshot(self.root)
        with patch('player_permissions.atomic_json', side_effect=PermissionError('locked')), self.assertRaises(PermissionError):
            set_player_role(self.root, '123', 'operator', snapshot['revision'])
        self.assertEqual((self.root / 'permissions.json').read_bytes(), snapshot['raw'])

    def test_queue_preview_handles_spaces_unicode_quotes_and_json(self):
        self.assertEqual(render_queue_command('Player Two', '/give {player} bread 3'), 'give "Player Two" bread 3')
        self.assertEqual(render_queue_command('Alex', 'say "hello @s there"'), 'say "hello @s there"')
        self.assertEqual(render_queue_command('Alex', 'tellraw @s {"rawtext":[{"text":"hello @s there"}]}'),
                         'tellraw "Alex" {"rawtext":[{"text":"hello @s there"}]}')
        self.assertEqual(render_queue_command('Alex', 'test @s[tag=staff]'), 'test @s[tag=staff]')
        self.assertEqual(render_queue_command('Álex', 'give @s bread'), 'give "Álex" bread')

    def test_invalid_queue_commands_rejected(self):
        for command in ('', '\nstop', 'say hi\rstop', 'say\x00x', 'x'*4097, '!restart', '/admin:heal'):
            with self.assertRaises(ValueError):
                self.h.queue_add('Offline Player', command)
        self.assertEqual(self.h.queue_for(), [])

    def test_queue_persists_and_is_bound_to_saved_identity(self):
        self.h.queue_add('Offline Player', 'give @s bread')
        other = PlayerHistory(self.root)
        self.addCleanup(other.close)
        self.h.player_joined('Offline Player', '999')
        send = Mock(return_value=True)
        self.assertEqual(other.queue_deliver('Offline Player', send), [])
        send.assert_not_called()
        self.assertEqual(len(other.queue_for('Offline Player')), 1)

    def test_command_created_after_join_waits_for_next_join(self):
        self.h.queue_add('Offline Player', 'give @s bread')
        watermark = self.h.queue_watermark()
        self.h.queue_add('Offline Player', 'give @s apple')
        send = Mock(return_value=True)
        self.h.queue_deliver('Offline Player', send, before_id=watermark)
        send.assert_called_once_with('give "Offline Player" bread')
        self.assertEqual(self.h.queue_for()[0]['command'], 'give @s apple')

    def test_cancel_pending_is_scoped_and_never_undoes_sent(self):
        item = self.h.queue_add('Offline Player', 'say hi')
        self.assertFalse(self.h.queue_delete(item, player='Someone Else'))
        self.assertTrue(self.h.queue_delete(item, player='Offline Player'))
        item = self.h.queue_add('Offline Player', 'say hi')
        self.h.queue_deliver('Offline Player', lambda cmd: True)
        self.assertFalse(self.h.queue_delete(item, player='Offline Player'))

    def test_two_connections_cannot_send_the_same_queue_twice(self):
        self.h.queue_add('Offline Player', 'give @s bread')
        other = PlayerHistory(self.root)
        self.addCleanup(other.close)
        sent, errors = [], []
        barrier = threading.Barrier(2)
        def work(history):
            try:
                barrier.wait(timeout=3)
                history.queue_deliver('Offline Player', lambda cmd: sent.append(cmd) or time.sleep(.05) or True)
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=work, args=(h,)) for h in (self.h, other)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(sent, ['give "Offline Player" bread'])

    def test_offline_admin_actions_do_not_change_last_seen(self):
        seen = self.h.players()[0]['last_seen']
        self.h.record('Offline Player', 'command', 'Role changed')
        self.h.queue_add('Offline Player', 'say hi')
        self.h.queue_deliver('Offline Player', lambda cmd: True)
        self.assertEqual(self.h.players()[0]['last_seen'], seen)

    def test_legacy_database_migration_preserves_pending_commands(self):
        self.h.close()
        with sqlite3.connect(self.root / 'players.db') as db:
            db.execute('ALTER TABLE queue DROP COLUMN xuid')
            db.execute("INSERT INTO queue(player, command, status) VALUES('Offline Player', 'say legacy', 'pending')")
        db.close()
        migrated = PlayerHistory(self.root)
        self.addCleanup(migrated.close)
        self.assertEqual(migrated.queue_for()[0]['command'], 'say legacy')
        self.assertEqual(migrated.queue_for()[0]['xuid'], '')

    def test_manager_role_rpc_saves_offline_role_and_requests_reload(self):
        runtime = Runtime.__new__(Runtime)
        runtime.root = self.root
        runtime.manager = SimpleNamespace(state_lock=threading.RLock(), stopping=threading.Event(),
                                          maintenance='', server_up=True, server=SimpleNamespace(send=Mock(return_value=True)))
        result = runtime.handle({'method': 'player_role', 'xuid': '123', 'role': 'operator', 'revision': 'missing'})
        self.assertTrue(result['reload_sent'])
        runtime.manager.server.send.assert_called_once_with('permission reload')
        self.assertEqual(permissions_snapshot(self.root)['rows'][0]['permission'], 'operator')
        runtime.manager.maintenance = 'Backup'
        with self.assertRaises(RuntimeError):
            runtime.handle({'method': 'player_role', 'xuid': '123', 'role': 'visitor', 'revision': 'missing'})


class DirectoryUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='flintdock-player-ui-')
        cls.root = Path(cls.temp.name)
        fixture(cls.root)
        cls.app = load_launcher().Launcher(cls.root, app_update_background=False)
        cls.errors = []
        cls.app.report_callback_exception = lambda *args: cls.errors.append(args)
        cls.app.history.player_joined('Offline Player', '123', '2026-01-01 10:00:00')
        cls.app.history.player_left('Offline Player', '2026-01-01 10:02:00')
        cls.app.history.player_joined('Online Player', '456')
        cls.app.players = {'Online Player'}
        cls.app.show_page('players')
        cls.app.update()

    @classmethod
    def tearDownClass(cls):
        cls.app.on_close()
        cls.temp.cleanup()

    def setUp(self):
        self.panel = self.app.player_directory
        self.app.show_page('players')
        self.panel.query.set('')
        self.panel.status.set('All players')
        self.panel.refresh()
        self.panel.tree.selection_set('Offline Player')
        self.panel.pick()
        self.app.update()

    def test_offline_selection_survives_refresh_and_filter_disables_actions(self):
        self.panel.refresh()
        self.assertEqual(self.panel.selected(), 'Offline Player')
        self.assertIn('Offline', self.panel.summary.get())
        self.assertGreater(len(self.panel.events.get_children()), 0)
        self.assertEqual(str(self.panel.quick_button['state']), 'disabled')
        self.panel.query.set('ONLINE')
        self.assertEqual(self.panel.tree.get_children(), ('Online Player',))
        self.assertIsNone(self.panel.selected())
        self.assertEqual(str(self.panel.role_button['state']), 'disabled')

    def test_queue_preview_confirm_cancel_and_next_join_copy(self):
        self.panel.command.set('give {player} bread 3')
        self.assertIn('give "Offline Player" bread 3', self.panel.preview.get())
        with patch('launcher_players.messagebox.askyesno', return_value=False):
            self.panel.add_queue()
        self.assertEqual(self.app.history.queue_for('Offline Player'), [])
        with patch('launcher_players.messagebox.askyesno', return_value=True):
            self.panel.add_queue()
            item = self.app.history.queue_for('Offline Player')[0]
            self.panel.queue_tree.selection_set(str(item['id']))
            self.panel.cancel_queue()
        self.assertEqual(self.app.history.queue_for('Offline Player'), [])

    def test_role_controls_save_offline_without_starting_server(self):
        self.panel.role.set('visitor')
        with patch('launcher_players.messagebox.askyesno', return_value=True), \
             patch.object(self.app.manager, 'running', return_value=False), \
             patch('bedrock_update.server_running', return_value=False), \
             patch.object(self.app.manager, 'start') as start:
            self.panel.save_role()
            deadline = time.monotonic() + 5
            while self.panel._busy and time.monotonic() < deadline:
                self.app.update()
                time.sleep(.02)
            self.assertFalse(self.panel._busy)
            start.assert_not_called()
        self.assertEqual(permissions_snapshot(self.root)['rows'][0]['permission'], 'visitor')
        self.assertIn('saved as visitor', self.panel.feedback.get())

    def test_activity_link_scopes_history_to_selected_offline_player(self):
        self.panel.open_activity()
        self.assertEqual(self.app.current_page, 'history')
        self.assertEqual(self.app.hist_player, 'Offline Player')
        self.assertEqual(self.app.hist_scope.get(), 'one')

    def test_controls_fit_supported_minimum_and_large_windows(self):
        for size in ('980x660', '1280x900'):
            self.app.geometry(size)
            for tab, widgets in ((self.panel.queue_tab, (self.panel.command_entry, self.panel.queue_button, self.panel.cancel_button)),
                                 (self.panel.role_tab, (self.panel.role_button, self.panel.role_select)),
                                 (self.panel.activity_tab, (self.panel.events,))):
                self.panel.tabs.select(tab)
                self.app.update()
                self.app.player_canvas.yview_moveto(1)
                self.app.update()
                for widget in widgets:
                    self.assertTrue(widget.winfo_ismapped(), (size, str(widget)))
                    self.assertGreaterEqual(widget.winfo_height(), 20, (size, str(widget), widget.winfo_height()))
                    self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), self.panel.winfo_rooty() + self.panel.winfo_height(), (size, str(widget)))
                    if widget is not self.panel.events:
                        canvas = self.app.player_canvas
                        self.assertGreaterEqual(widget.winfo_rooty(), canvas.winfo_rooty(), (size, str(widget)))
                        self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), canvas.winfo_rooty() + canvas.winfo_height(), (size, str(widget)))
        self.assertEqual(self.errors, [])
