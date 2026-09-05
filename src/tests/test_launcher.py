"""Tk integration tests against a disposable server folder; no live server starts."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_tooling import fixture, load_launcher
import bedrock_storage as storage

launcher = load_launcher()


class LauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='bedrock-ui-test-')
        cls.root = Path(cls.temp.name)
        fixture(cls.root)
        cls.app = launcher.Launcher(cls.root, app_update_background=False)
        cls.app.withdraw()
        cls.errors = []
        cls.app.report_callback_exception = lambda *args: cls.errors.append(args)
        cls.app.update()

    @classmethod
    def tearDownClass(cls):
        cls.app.on_close()
        cls.temp.cleanup()

    def test_all_pages_build_and_navigate(self):
        for page in self.app.NAV_ORDER:
            self.app.show_page(page)
            self.app.update()
            self.assertEqual(self.app.current_page, page)
        self.assertEqual(self.errors, [])

    @contextmanager
    def quick_ready(self):
        app = self.app
        app._quick_next_send = 0
        app._quick_busy = False
        with patch.object(app, 'players', {'Admin', 'Player Two'}), patch.object(app, 'server_up', True), \
                patch.object(app.manager, 'running', return_value=True), patch.object(app, '_stopping_on_purpose', False), \
                patch.object(app, '_maintenance', ''), patch.object(app, '_update_busy', False), patch.object(app, '_install_stage', False):
            try:
                yield app
            finally:
                win = getattr(app, '_quick_window', None)
                if win is not None and win.winfo_exists():
                    win.destroy()
                app._quick_busy = False
                app._quick_next_send = 0

    def drain_quick(self):
        deadline = time.time() + 5
        while self.app._quick_busy and time.time() < deadline:
            self.app.update()
            time.sleep(.02)
        self.assertFalse(self.app._quick_busy)

    def test_quick_window_search_favorites_preview_and_layout(self):
        with self.quick_ready() as app, patch.object(app.manager, 'send') as send:
            win = app.open_admin_quick_commands('heal', 'Player Two')
            app.update()
            self.assertEqual(len(win.tree.get_children()), 28)
            self.assertIn('effect "Player Two" instant_health', win.preview.get('1.0', 'end'))
            self.assertEqual(str(win.preview['state']), 'disabled')
            for geometry in ('780x610', '920x690'):
                win.geometry(geometry)
                app.update()
                self.assertTrue(win.send_button.winfo_ismapped())
                self.assertGreaterEqual(win.send_button.winfo_width(), win.send_button.winfo_reqwidth())
                self.assertGreater(win.preview.winfo_height(), 25)
                self.assertGreater(win.tree.winfo_height(), 100)
            win.query.set('teleport')
            app.update()
            self.assertEqual(win.tree.get_children(), ('teleport',))
            win.values['destination'].set('Admin')
            win.copy_command()
            self.assertEqual(app.clipboard_get(), 'tp "Player Two" "Admin" true')
            win.query.set('bread')
            app.update()
            win.toggle_favorite()
            self.assertIn('bread', json.loads((self.root / 'launcher_ui.json').read_text())['admin_quick_favorites'])
            win.query.set('zzzznothing')
            app.update()
            self.assertEqual(win.tree.get_children(), ())
            self.assertEqual(str(win.send_button['state']), 'disabled')
            send.assert_not_called()

    def test_quick_send_runs_off_ui_thread_and_reports_dispatch_not_execution(self):
        with self.quick_ready() as app:
            win = app.open_admin_quick_commands('heal', 'Player Two')
            main_thread = threading.get_ident()
            threads = []
            with patch.object(app.manager, 'send', side_effect=lambda cmd: threads.append(threading.get_ident()) or True) as send:
                self.assertTrue(app.run_admin_quick_command('heal', {'player': 'Player Two'}))
                self.assertFalse(app.run_admin_quick_command('heal', {'player': 'Player Two'}))
                self.drain_quick()
                send.assert_called_once_with('effect "Player Two" instant_health 1 10 true')
                self.assertNotEqual(threads[0], main_thread)
                self.assertIn('not execution confirmation', win.result.get())
                self.assertFalse(app.run_admin_quick_command('heal', {'player': 'Player Two'}))
                send.assert_called_once()

    def test_quick_confirmation_cancel_and_disconnecting_target_send_nothing(self):
        with self.quick_ready() as app, patch.object(app.manager, 'send') as send:
            app.open_admin_quick_commands('kick', 'Player Two')
            with patch('launcher_features.messagebox.askyesno', return_value=False):
                self.assertFalse(app.run_admin_quick_command('kick', {'player': 'Player Two', 'reason': 'Please rejoin'}))
            def disconnect(*args, **kwargs):
                app.players.remove('Player Two')
                return True
            with patch('launcher_features.messagebox.askyesno', side_effect=disconnect):
                self.assertFalse(app.run_admin_quick_command('teleport', {'player': 'Player Two', 'destination': 'Admin'}))
            app._quick_window.tick()
            self.assertEqual(app._quick_window.values['player'].get(), '')
            send.assert_not_called()
            self.assertFalse(app._quick_busy)

    def test_quick_confirmation_uses_original_preview_and_rechecks_server_state(self):
        with self.quick_ready() as app, patch.object(app.manager, 'send') as send:
            win = app.open_admin_quick_commands('day')
            def stop(*args, **kwargs):
                self.assertIn('time set day', args[1])
                app.server_up = False
                return True
            with patch('launcher_features.messagebox.askyesno', side_effect=stop):
                self.assertFalse(app.run_admin_quick_command('day', {}))
            send.assert_not_called()
            self.assertIn('Start the server', win.result.get())

    def test_quick_failed_delivery_is_not_retried(self):
        with self.quick_ready() as app, patch.object(app.manager, 'send', return_value=False) as send:
            win = app.open_admin_quick_commands('list')
            self.assertTrue(app.run_admin_quick_command('list', {}))
            self.drain_quick()
            send.assert_called_once_with('list')
            self.assertIn('not confirmed', win.result.get())
            self.assertIn('Nothing was retried', win.result.get())

    def test_quick_maintenance_and_offline_server_never_queue_commands(self):
        with self.quick_ready() as app, patch.object(app.manager, 'send') as send:
            app._maintenance = 'Backup'
            self.assertFalse(app.run_admin_quick_command('day', {}))
            app._maintenance = ''
            app.server_up = False
            self.assertFalse(app.run_admin_quick_command('list', {}))
            send.assert_not_called()

    def test_command_help_search_copy_and_read_only_details(self):
        app = self.app
        win = app.command_help_dialog()
        deadline = time.time() + 5
        while not win.by_id and time.time() < deadline:
            app.update()
            time.sleep(0.02)
        self.assertEqual(len(win.by_id), 47)
        self.assertIs(app.command_help_dialog(), win)
        for geometry in ('760x520', '1000x700'):
            win.geometry(geometry)
            app.update()
            self.assertGreater(win.tree.winfo_height(), 100)
            self.assertGreater(win.details.winfo_width(), 250)
        win.query.set('stop')
        app.update()
        self.assertIn('core:stop', win.tree.get_children())
        win.tree.selection_set('core:stop')
        app.update()
        self.assertIn('Server console only', win.details.get('1.0', 'end'))
        self.assertEqual(str(win.details['state']), 'disabled')
        app._copy_command_help(win, True)
        self.assertEqual(app.clipboard_get(), 'stop')
        win.query.set('zzzznothing')
        app.update()
        self.assertEqual(win.tree.get_children(), ())
        self.assertIn('No matching', win.details.get('1.0', 'end'))
        win.destroy()

    def test_install_ingame_tools_completes_through_real_worker_queue(self):
        import build_admin_addon as builder
        app = self.app
        with patch.object(app.manager, 'running', return_value=False), patch.object(app.stats, 'found', False), \
                patch('launcher_features.messagebox.askyesno', return_value=True), \
                patch.object(builder, 'server_running', return_value=False), \
                patch.object(launcher.bedrock_update, 'server_running', return_value=False):
            self.assertTrue(app.install_ingame_tools())
            deadline = time.time() + 10
            while app._maintenance and time.time() < deadline:
                app.update()
                time.sleep(0.03)
            self.assertEqual(app._maintenance, '')
        self.assertTrue((app.root_dir / builder.INSTALLED / 'scripts/help.js').is_file())
        self.assertIn('verified/updated', app.task_status.get())
        self.assertIn('v1.9.0 registered', app.admin_tools_status.get())

    def test_install_ingame_tools_uses_locked_background_maintenance(self):
        import build_admin_addon as builder
        app = self.app
        calls = []
        def maintenance(label, work, readonly=False):
            calls.append(label)
            calls.append(work())
            return True
        with patch.object(app.manager, 'running', return_value=False), patch.object(app.stats, 'found', False), \
                patch('launcher_features.messagebox.askyesno', return_value=True), \
                patch.object(app, '_maintenance_task', side_effect=maintenance), \
                patch.object(builder, 'install') as install:
            self.assertTrue(app.install_ingame_tools())
            install.assert_called_once_with(app.root_dir, lock_held=True, progress=app.report_stage,
                                            enable=True, rebuild_catalog=True)
        self.assertEqual(calls[0], 'Install / update in-game tools')

    def test_install_ingame_tools_cancel_or_running_server_changes_nothing(self):
        import build_admin_addon as builder
        app = self.app
        with patch.object(builder, 'install') as install, patch.object(app.manager, 'running', return_value=True), \
                patch('launcher_features.messagebox.showwarning'):
            self.assertFalse(app.install_ingame_tools())
            install.assert_not_called()
        with patch.object(builder, 'install') as install, patch.object(app.manager, 'running', return_value=False), \
                patch.object(app.stats, 'found', False), patch('launcher_features.messagebox.askyesno', return_value=False):
            self.assertFalse(app.install_ingame_tools())
            install.assert_not_called()

    def test_save_schedule_keeps_backup_preferences(self):
        storage.atomic_json(self.root / 'manager_config.json', {
            'backup_before_restart': True, 'backup_keep': 17, 'custom': 'keep'})
        self.app.sched_entry.delete(0, 'end')
        self.app.sched_entry.insert(0, '03:00, 18:00')
        self.app.save_schedule()
        result = json.loads((self.root / 'manager_config.json').read_text())
        self.assertTrue(result['backup_before_restart'])
        self.assertEqual(result['backup_keep'], 17)
        self.assertEqual(result['custom'], 'keep')

    def test_console_repeat_collapses_across_blank_lines(self):
        self.app.clear_console()
        for _ in range(4):
            self.app.log_line('[2026-09-04 12:00:00 ERROR] Broken structure')
            self.app.log_line('')
            self.app.log_line('')
        errors = [r for r in self.app.console_buffer if 'Broken structure' in r['text']]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]['n'], 4)

    def test_backup_finishes_through_ui_queue(self):
        before = len(list((self.root / 'backups').glob('*.zip')))
        with patch.object(launcher.bedrock_update, 'server_running', return_value=False):
            self.assertTrue(self.app.backup_now())
            until = time.monotonic() + 10
            while self.app._maintenance and time.monotonic() < until:
                self.app.update()
                time.sleep(.01)
            self.assertEqual(self.app._maintenance, '')
            self.assertEqual(len(list((self.root / 'backups').glob('*.zip'))), before + 1)

    def test_single_named_timer_and_font_scaling(self):
        self.app._repeat('test', 1000, lambda: None)
        previous = self.app._timers['test']
        self.app._repeat('test', 1000, lambda: None)
        self.assertNotIn(previous, self.app.tk.splitlist(self.app.tk.call('after', 'info')))
        self.app.set_scale(1.3)
        self.app.update()
        self.app.set_scale(1)
        self.assertEqual(self.errors, [])

    def test_settings_preview_leaves_disk_unchanged_until_saved(self):
        self.app.load_props()
        before = (self.root / 'server.properties').read_text()
        self.app.prop_vars['max-players'].set('15')
        self.app.save_props()
        self.app.update()
        self.assertEqual((self.root / 'server.properties').read_text(), before)
        dialogs = [w for w in self.app.winfo_children() if isinstance(w, launcher.tk.Toplevel)]
        review = next(w for w in dialogs if w.title() == 'Review settings changes')
        next(w for w in review.winfo_children() if isinstance(w, launcher.ttk.Button)).invoke()
        self.app.update()
        self.assertIn('max-players=15', (self.root / 'server.properties').read_text())
        from bedrock_experience import undo_settings
        undo_settings(self.root)
        self.app.load_props()

    def test_history_refresh_keeps_selection_and_sort_in_single_player_scope(self):
        self.app.history.player_joined('Alex')
        self.app.history.record('Alex', 'chat', 'Test')
        self.app.refresh_history()
        tree = self.app.hist_tree
        self.app.hist_scope.set('one')
        tree.selection_set(next(i for i in tree.get_children() if tree.item(i, 'text').strip() == 'Alex'))
        self.app.update()
        self.app.sort_tree(tree, 'hist_tree', '#0')
        self.app.refresh_history()
        self.app.update()
        self.assertEqual(tree.item(tree.selection()[0], 'text').strip(), 'Alex')
        self.assertEqual(self.app._sort_preferences['hist_tree'][0], '#0')
        self.app.hist_scope.set('all')

    def test_backup_filter_and_labels_keep_selection(self):
        from bedrock_experience import label_backup
        path = storage.create_backup(self.root)
        self.app.refresh_backups()
        tree = self.app.backup_tree
        tree.selection_set(next(i for i, p in self.app._backups.items() if p == path))
        label_backup(self.root, path, 'Castle checkpoint')
        self.app.refresh_backups()
        self.assertEqual(tree.item(tree.selection()[0], 'text'), 'Castle checkpoint')
        self.app.backup_filter.set('Castle')
        self.app.refresh_backups()
        self.assertEqual(len(tree.get_children()), 1)
        self.app.backup_filter.set('')
        self.app.refresh_backups()

    def test_palette_builds_and_filters(self):
        self.app.action_palette()
        self.app.update()
        children = self.app._palette.winfo_children()
        search = next(c for c in children if isinstance(c, launcher.ttk.Entry))
        choices = next(c for c in children if isinstance(c, launcher.tk.Listbox))
        search.insert(0, 'backup')
        search.event_generate('<KeyRelease>', keysym='p')
        self.app.update()
        self.assertTrue(choices.size() >= 1)
        self.app._palette.destroy()

    def test_primary_controls_remain_visible(self):
        self.app.deiconify()
        for geometry in ('1280x900', '980x740'):
            self.app.geometry(geometry)
            for page, control in (('console', self.app.entry), ('history', self.app.q_entry),
                                   ('schedule', self.app.sched_entry), ('backups', self.app.backup_tree)):
                self.app.show_page(page)
                self.app.update()
                with self.subTest(geometry=geometry, page=page):
                    self.assertTrue(control.winfo_ismapped())
                    self.assertGreater(control.winfo_width(), 25)
                    self.assertGreater(control.winfo_height(), 10)
                    self.assertLess(control.winfo_rooty(), self.app.winfo_rooty() + self.app.winfo_height())
        self.app.withdraw()

    def test_visual_shell_navigation_and_content_space(self):
        self.app.deiconify()
        for geometry in ('980x660', '980x740', '1280x900'):
            self.app.geometry(geometry)
            self.app.show_page('dashboard')
            self.app.update()
            with self.subTest(geometry=geometry):
                self.assertEqual(self.app._stat_columns, 6)
                self.assertGreater(self.app.health_canvas.winfo_height(), 45)
                for key, item in self.app.nav.items():
                    self.assertTrue(item.winfo_ismapped(), key)
                    self.assertGreaterEqual(item.winfo_height(), 30, key)
                    self.assertLessEqual(item.winfo_rooty()+item.winfo_height(),
                                         self.app.winfo_rooty()+self.app.winfo_height(), key)
                self.assertEqual(self.app.btn_start._text, 'Ignite server')
        self.app.withdraw()

    def test_page_actions_are_not_clipped(self):
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        self.app.deiconify()
        for geometry in ('980x740', '1280x900'):
            self.app.geometry(geometry)
            for page in self.app.NAV_ORDER:
                self.app.show_page(page)
                self.app.update()
                for button in descendants(self.app.pages[page]):
                    if (not isinstance(button, launcher.RoundButton) or not button.winfo_manager()
                            or not button.master.winfo_ismapped()):
                        continue
                    with self.subTest(geometry=geometry, page=page, button=button._text):
                        self.assertTrue(button.winfo_ismapped())
                        self.assertGreaterEqual(button.winfo_width(), int(button['width'])-2)
                        self.assertGreaterEqual(button.winfo_height(), int(button['height'])-2)
        self.app.withdraw()

    def test_theme_text_has_readable_contrast(self):
        from launcher_theme import FG, FG_DIM, FG_FAINT, PANEL, CARD, SIDEBAR
        def luminance(color):
            values = [int(color[i:i+2], 16)/255 for i in (1, 3, 5)]
            linear = [v/12.92 if v <= .04045 else ((v+.055)/1.055)**2.4 for v in values]
            return sum(v*k for v, k in zip(linear, (.2126, .7152, .0722)))
        for foreground in (FG, FG_DIM, FG_FAINT):
            for background in (PANEL, CARD, SIDEBAR):
                self.assertGreaterEqual((luminance(foreground)+.05)/(luminance(background)+.05), 4.5)

    def test_console_filters_wrap_with_large_counts(self):
        self.app.deiconify()
        self.app.geometry('980x740')
        self.app.show_page('console')
        for chip in self.app.level_chips.values():
            chip.set_count(1234)
        self.app.update()
        flow = next(iter(self.app.level_chips.values())).master
        for item in flow.items:
            self.assertLessEqual(item.winfo_x()+item.winfo_width(), flow.winfo_width())
        self.assertGreater(self.app.console.winfo_height(), 65)
        self.app._paint_counts()
        self.app.withdraw()

    def test_empty_console_message_hides_when_output_arrives(self):
        self.app.clear_console()
        self.assertTrue(self.app.console_empty.winfo_manager())
        self.app.log_line('A visible test line', 'cmd')
        self.assertFalse(self.app.console_empty.winfo_manager())


def preview():
    """A visual QA window with an isolated demo world and automatic cleanup."""
    with tempfile.TemporaryDirectory(prefix='bedrock-preview-') as folder:
        root = Path(folder)
        fixture(root)
        storage.atomic_json(root / 'manager_config.json', {
            'restart_times': ['06:00', '14:00', '22:00'], 'backup_before_restart': True})
        app = launcher.Launcher(root, app_update_background=False)
        app.title('FlintDock — Visual QA (demo world)')
        app.geometry('1280x900+30+30')
        app.server_version = '1.26.45.1'
        app.log_line('[launcher] Demo workspace. Live server files are not used.', 'mgr')
        app.after(600000, app.on_close)
        app.mainloop()


if __name__ == '__main__':
    if '--preview' in sys.argv:
        preview()
    else:
        unittest.main()
