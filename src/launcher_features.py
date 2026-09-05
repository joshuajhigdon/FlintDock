"""Optional UI workflows kept separate from the launcher's console renderer."""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog

import bedrock_experience as experience
import bedrock_mods as mods
import bedrock_recovery as recovery
from bedrock_storage import atomic_json, atomic_text, operation_lock
from launcher_theme import BG, PANEL, CARD, INPUT, LINE, FG, FG_DIM, FG_FAINT, GREEN, BLUE, PURPLE, SELECTED


class FeatureMixin:
    def install_features(self):
        self._features_ready = True
        self._task_cancel = threading.Event()
        self._samples = deque(maxlen=200)
        self._task_phase = ''
        self._sort_preferences = self.ui.get('table_sorts', {})
        if not isinstance(self._sort_preferences, dict):
            self._sort_preferences = {}
        menu = tk.Menu(self, tearoff=False)
        tools = tk.Menu(menu, tearoff=False)
        for label, action in [('Complete restore point…', self.complete_backup),
                              ('Recover interrupted operation…', self.recovery_dialog),
                              ('Rehearse selected update…', self.rehearse_update),
                              ('Troubleshooting report…', self.incident_dialog),
                              ('Cancel current preparation', self.cancel_preparation)]:
            tools.add_command(label=label, command=action)
        tools.add_separator()
        tools.add_command(label='Admin quick commands…', command=self.open_admin_quick_commands)
        menu.add_cascade(label='Tools', menu=tools)
        mod_menu = tk.Menu(menu, tearoff=False)
        mod_menu.add_command(label='Profiles and comparison…', command=self.profiles_dialog)
        mod_menu.add_command(label='Check dependencies…', command=self.dependencies_dialog)
        mod_menu.add_separator()
        mod_menu.add_command(label='Install / update in-game tools…', command=self.install_ingame_tools)
        menu.add_cascade(label='Mods', menu=mod_menu)
        help_menu = tk.Menu(menu, tearoff=False)
        help_menu.add_command(label='Server command reference…', command=self.command_help_dialog)
        help_menu.add_command(label='Admin quick commands…', command=self.open_admin_quick_commands)
        help_menu.add_command(label='Install / update in-game tools…', command=self.install_ingame_tools)
        help_menu.add_command(label='FlintDock updates…', command=lambda: self.app_updates.show())
        menu.add_cascade(label='Help', menu=help_menu)
        settings = tk.Menu(menu, tearoff=False)
        settings.add_command(label='Preview settings changes…', command=self.save_props)
        settings.add_command(label='Undo last settings change…', command=self.undo_props)
        settings.add_command(label='Settings history…', command=self.settings_history_dialog)
        settings.add_command(label='Maintenance scheduling…', command=self.maintenance_options)
        settings.add_command(label='FlintDock updates…', command=lambda: self.app_updates.show())
        menu.add_cascade(label='Settings', menu=settings)
        self.configure(menu=menu)
        self.feature_menu = menu

        page = self.pages['backups']
        controls = tk.Frame(page, bg=PANEL)
        controls.pack(fill='x', padx=20, pady=(0, 8), before=page.winfo_children()[1])
        ttk.Label(controls, text='Filter').pack(side='left')
        self.backup_filter = tk.StringVar()
        ttk.Entry(controls, textvariable=self.backup_filter, width=20).pack(side='left', padx=8)
        self.backup_kind_filter = tk.StringVar(value='all')
        ttk.Combobox(controls, textvariable=self.backup_kind_filter, state='readonly', width=13,
                     values=['all', 'manual', 'auto', 'restore-point', 'replaced', 'preupdate', 'legacy']).pack(side='left')
        ttk.Button(controls, text='Label selected', command=self.label_selected_backup).pack(side='left', padx=8)
        ttk.Button(controls, text='Restore point', command=self.complete_backup).pack(side='left')
        self.backup_filter.trace_add('write', lambda *_: self._repeat('backup_filter', 150, self.refresh_backups))
        self.backup_kind_filter.trace_add('write', lambda *_: self.refresh_backups())
        self.backup_tree.configure(columns=('world', 'kind', 'when', 'size', 'verified'))
        for key, title, width in [('world', 'WORLD', 110), ('kind', 'TYPE', 105), ('when', 'CREATED', 145),
                                   ('size', 'SIZE', 75), ('verified', 'VERIFIED', 145)]:
            self.backup_tree.heading(key, text=title)
            self.backup_tree.column(key, width=width, minwidth=55, stretch=True)
        self.backup_tree.column('#0', width=240, minwidth=140)

        parent = self.performance_host
        self.trend_canvas = tk.Canvas(parent, width=1, height=142, background=CARD, highlightthickness=0)
        self.trend_canvas.pack(fill='x', padx=12, pady=(0, 8))
        self.trend_canvas.bind('<Configure>', lambda _e: self.draw_trends())
        parent = self.overview_label.master
        self.backup_health_line = tk.Label(parent, text='', bg=CARD, fg=FG_DIM, anchor='w', font=('Segoe UI', 9))
        self.backup_health_line.pack(fill='x', padx=16, pady=(0, 10))
        actions = tk.Frame(parent, bg=CARD)
        actions.pack(fill='x', padx=12, pady=(0, 10))
        ttk.Button(actions, text='Troubleshoot', command=self.incident_dialog).pack(side='left')
        ttk.Button(actions, text='Browse backups', command=lambda: self.show_page('backups')).pack(side='left', padx=8)
        for name in ('backup_tree', 'mod_tree', 'arch_tree', 'hist_tree', 'ev_tree', 'version_tree'):
            tree = getattr(self, name, None)
            if tree is not None:
                self.configure_sorting(tree, name)
        self.refresh_backups()
        self._repeat('feature_tick', 3000, self.feature_tick)
        pending = recovery.operations(self.root_dir, True)
        if pending:
            self.notify('error', 'An interrupted operation needs recovery',
                        'Open Tools → Recover interrupted operation before starting the server.', 'recovery')

    def configure_sorting(self, tree, name):
        if not hasattr(self, '_heading_titles'):
            self._heading_titles = {}
        columns = list(tree['columns'])
        if 'tree' in str(tree['show']):
            columns.insert(0, '#0')
        for column in columns:
            self._heading_titles[name, column] = tree.heading(column, 'text')
            tree.heading(column, command=lambda c=column: self.sort_tree(tree, name, c))
        self.stripe_tree(tree)

    def open_admin_quick_commands(self, preset_id=None, player=None):
        from launcher_quick_commands import QuickCommandWindow
        if player is None:
            player = (self.player_directory.selected() or '') if self.current_page == 'players' else ''
        win = getattr(self, '_quick_window', None)
        if win is not None and win.winfo_exists():
            if player in self.players:
                win.values['player'].set(player)
            if preset_id:
                win.query.set('')
                win.category.set('All categories')
                win.favorites_only.set(False)
                win.filter(preset_id)
            win.lift()
            return win
        self._quick_window = QuickCommandWindow(self, preset_id, player)
        return self._quick_window

    def _quick_feedback(self, text):
        win = getattr(self, '_quick_window', None)
        if win is not None and win.winfo_exists():
            win.result.set(text)
            win.update_preview()
        else:
            self.log_line('[quick commands] ' + text, 'mgr')

    def run_admin_quick_command(self, preset_id, values):
        """Reserve one request, confirm its immutable preview, then send off the UI thread."""
        from admin_quick_commands import prepare, BY_ID, blocked_reason
        if getattr(self, '_quick_busy', False):
            self._quick_feedback('A quick command is already being reviewed or sent. Wait for its result.')
            return False
        if time.monotonic() < getattr(self, '_quick_next_send', 0):
            self._quick_feedback('Please wait a second before sending another quick command.')
            return False
        values = dict(values)
        try:
            if reason := blocked_reason(self):
                raise ValueError(reason)
            command = prepare(preset_id, values, self.players.copy())
        except ValueError as exc:
            self._quick_feedback(str(exc))
            return False
        preset = BY_ID[preset_id]
        self._quick_busy = True
        launched = False
        try:
            if preset.confirm:
                win = getattr(self, '_quick_window', None)
                parent = win if win is not None and win.winfo_exists() else self
                if not messagebox.askyesno('Confirm admin command',
                        f'{preset.label}\n\n{preset.description}\n\nExact command:\n{command}\n\nSend this request now?', parent=parent):
                    self._quick_feedback('Cancelled. Nothing was sent.')
                    return False
            # Modal confirmation keeps processing UI events. Do not trust the old roster/state.
            if reason := blocked_reason(self):
                raise ValueError(reason)
            if prepare(preset_id, values, self.players.copy()) != command:
                raise ValueError('The command changed during confirmation. Review it again.')
            self._quick_feedback('Sending request…')

            def worker():
                sent, error = False, ''
                try:
                    if reason := blocked_reason(self):
                        raise ValueError(reason)
                    prepare(preset_id, values, self.players.copy())
                    sent = bool(self.manager.send(command))
                    if not sent:
                        error = 'Delivery was not confirmed. Nothing was retried; check Console before trying again.'
                except Exception as exc:
                    error = str(exc)
                self.q.put(('quick_command_done', (preset_id, command, values, sent, error)))

            threading.Thread(target=worker, daemon=True).start()
            launched = True
            return True
        except ValueError as exc:
            self._quick_feedback(str(exc))
            return False
        finally:
            if not launched:
                self._quick_busy = False
                win = getattr(self, '_quick_window', None)
                if win is not None and win.winfo_exists():
                    win.update_preview()

    def admin_quick_command_done(self, preset_id, command, values, sent, error):
        from admin_quick_commands import BY_ID
        self._quick_busy = False
        self._quick_next_send = time.monotonic() + 1
        preset = BY_ID[preset_id]
        if sent:
            self.log_line(f'[quick commands] Sent {preset.label}: {command}', 'mgr')
            self._quick_feedback(f'{preset.label}: sent to manager. Check Console for the server result; this is not execution confirmation.')
            if values.get('player') and self.history:
                try:
                    self.history.record(values['player'], 'command', f'[Launcher quick] Request sent: {command}')
                except Exception as exc:
                    self.log_line(f'[quick commands] Request sent but player-history logging failed: {exc}', 'err')
        else:
            self.log_line(f'[quick commands] {preset.label}: {error}', 'err')
            self._quick_feedback(error)

    def stripe_tree(self, tree):
        tree.tag_configure('row_even', background=INPUT)
        tree.tag_configure('row_odd', background='#1c1527')
        for i, item in enumerate(tree.get_children()):
            tags = [t for t in tree.item(item, 'tags') if t not in ('row_even', 'row_odd')]
            tree.item(item, tags=(*tags, 'row_odd' if i % 2 else 'row_even'))

    def sort_tree(self, tree, name, column=None):
        previous = self._sort_preferences.get(name, ['', False])
        if not isinstance(previous, list) or len(previous) != 2:
            previous = ['', False]
        if column is None:
            column, reverse = previous
        else:
            reverse = not previous[1] if previous[0] == column else False
        if not column:
            return
        def key(item):
            value = str(tree.item(item, 'text') if column == '#0' else tree.set(item, column)).strip()
            if name == 'version_tree' and column == tree['columns'][0]:
                try:
                    return (0, tuple(int(part) for part in value.split('.')))
                except ValueError:
                    return (1, value.casefold())
            try:
                return (0, float(value.split()[0]))
            except (ValueError, IndexError):
                return (1, value.casefold())
        try:
            for i, item in enumerate(sorted(tree.get_children(), key=key, reverse=reverse)):
                tree.move(item, '', i)
        except tk.TclError:
            return
        self._sort_preferences[name] = [column, reverse]
        self.ui['table_sorts'] = self._sort_preferences
        self.stripe_tree(tree)
        for (table, key), title in getattr(self, '_heading_titles', {}).items():
            if table == name:
                tree.heading(key, text=title + (' ↓' if reverse else ' ↑') if key == column else title)

    @contextmanager
    def preserve_tree(self, tree, name):
        def identity(item):
            if name == 'backup_tree':
                return getattr(self, '_backups', {}).get(item, item)
            if name == 'version_tree':
                return tuple(tree.item(item, 'values'))[:1]
            return tree.item(item, 'text') or tuple(tree.item(item, 'values'))
        selected = [identity(item) for item in tree.selection()]
        position = tree.yview()
        yield
        for item in tree.get_children():
            if identity(item) in selected and item not in tree.selection():
                tree.selection_add(item)
        if getattr(self, '_features_ready', False):
            self.sort_tree(tree, name)
            self.stripe_tree(tree)
        if position:
            tree.yview_moveto(position[0])

    def refresh_mods(self):
        with self.preserve_tree(self.mod_tree, 'mod_tree'), self.preserve_tree(self.arch_tree, 'arch_tree'):
            self._refresh_mods_legacy()
        if hasattr(self, 'admin_tools_status'):
            try:
                from build_admin_addon import status
                self.admin_tools_status.set(status(self.root_dir))
            except Exception as exc:
                self.admin_tools_status.set(f'In-game tooling unavailable: {exc}')

    def install_ingame_tools(self):
        if self.manager.running() or self.stats.found:
            messagebox.showwarning('In-game tools', 'Stop the server first. Then install/update the tools and start normally.')
            return False
        try:
            from build_admin_addon import install, compatibility, status
            note = compatibility(self.root_dir)
        except (OSError, ValueError, RuntimeError, ImportError, KeyError, TypeError) as exc:
            messagebox.showerror('In-game tools', str(exc))
            return False
        if not messagebox.askyesno('Install / update in-game tools', status(self.root_dir) + '\n\n' + note +
                '\n\nInstall, repair and ENABLE only the bundled Restart Manager Link addon?'
                '\nThis refreshes command help from installed packs. Other mods stay as configured.'
                '\nChanged installs get a complete restore point. No download, cheats setting or permissions change.'
                '\n\nAfterward start with Launcher.bat and use /admin:help as an operator.'):
            return False
        def work():
            install(self.root_dir, lock_held=True, progress=self.report_stage, enable=True, rebuild_catalog=True)
            return 'In-game tools verified/updated. Start normally; operators can open /admin:help or /admin:menu.'
        return self._maintenance_task('Install / update in-game tools', work)

    def command_help_dialog(self):
        """Searchable documentation, intentionally disconnected from command dispatch."""
        existing = getattr(self, '_help_window', None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            return existing
        win = tk.Toplevel(self)
        self._help_window = win
        win.title('Server command reference')
        win.geometry('1000x700')
        win.minsize(760, 520)
        win.configure(bg=PANEL)
        tk.Label(win, text='COMMAND REFERENCE', bg=PANEL, fg=PURPLE,
                 font=('Segoe UI', 16, 'bold'), anchor='w').pack(fill='x', padx=18, pady=(16, 4))
        tk.Label(win, text='Core server · Admin tools · Restart manager · Installed mods  |  In game: /admin:help',
                 bg=PANEL, fg=FG_DIM, anchor='w').pack(fill='x', padx=18)
        filters = tk.Frame(win, bg=PANEL)
        filters.pack(fill='x', padx=18, pady=12)
        win.query = tk.StringVar()
        win.category = tk.StringVar(value='All commands')
        ttk.Label(filters, text='Search').pack(side='left')
        search = ttk.Entry(filters, textvariable=win.query)
        search.pack(side='left', fill='x', expand=True, padx=8)
        ttk.Combobox(filters, textvariable=win.category, width=19, state='readonly',
                     values=['All commands', 'Core server', 'Admin tools', 'Restart manager', 'Installed mods']).pack(side='left')
        win.refresh = ttk.Button(filters, text='Refresh from packs', command=lambda: self._load_command_help(win))
        win.refresh.pack(side='left', padx=(8, 0))
        win.summary = tk.StringVar(value='Loading reference and scanning installed pack documentation…')
        summary = tk.Label(win, textvariable=win.summary, bg=PANEL, fg=FG_DIM, justify='left', anchor='w', wraplength=940)
        summary.pack(fill='x', padx=18, pady=(0, 10))
        summary.bind('<Configure>', lambda e: summary.configure(wraplength=max(400, e.width - 8)))
        split = tk.PanedWindow(win, bg=PANEL, sashwidth=8, borderwidth=0)
        split.pack(fill='both', expand=True, padx=18)
        left, right = tk.Frame(split, bg=PANEL), tk.Frame(split, bg=PANEL)
        split.add(left, minsize=235, width=330)
        split.add(right, minsize=360)
        win.tree = ttk.Treeview(left, show='tree', selectmode='browse')
        scroll = ttk.Scrollbar(left, command=win.tree.yview)
        win.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        win.tree.pack(fill='both', expand=True)
        win.details = tk.Text(right, wrap='word', bg=INPUT, fg=FG, relief='flat', padx=14, pady=12,
                              font=('Segoe UI', 11), state='disabled', width=35)
        detail_scroll = ttk.Scrollbar(right, command=win.details.yview)
        win.details.configure(yscrollcommand=detail_scroll.set)
        detail_scroll.pack(side='right', fill='y')
        win.details.pack(fill='both', expand=True)
        buttons = tk.Frame(win, bg=PANEL)
        buttons.pack(fill='x', padx=18, pady=14)
        ttk.Button(buttons, text='Copy example', command=lambda: self._copy_command_help(win, True)).pack(side='left')
        ttk.Button(buttons, text='Copy reference', command=lambda: self._copy_command_help(win, False)).pack(side='left', padx=8)
        tk.Label(buttons, text='Read-only • Nothing here executes commands', bg=PANEL, fg=FG_DIM).pack(side='left', padx=8)
        ttk.Button(buttons, text='Close', command=win.destroy).pack(side='right')
        win.reference = {'entries': [], 'packs': [], 'warnings': []}
        win.by_id = {}
        win.tree.bind('<<TreeviewSelect>>', lambda _e: self._show_command_help_entry(win))
        win.query.trace_add('write', lambda *_: self._filter_command_help(win))
        win.category.trace_add('write', lambda *_: self._filter_command_help(win))
        search.focus_set()
        self._load_command_help(win)
        return win

    def _load_command_help(self, win):
        win.refresh.configure(state='disabled')
        win.summary.set('Reading documentation (no pack code is executed)…')
        def worker():
            try:
                from command_help import build_reference
                result, error = build_reference(self.root_dir), None
            except Exception as exc:
                # A broken pack/state file must not hide the bundled command help.
                error = str(exc)
                try:
                    from command_help import build_reference
                    result = build_reference()
                except Exception as fallback:
                    result = {'entries': [], 'packs': [], 'warnings': ['Restore the bundled command_reference.json and helper scripts.']}
                    error += f'; bundled reference unavailable: {fallback}'
            self.q.put(('command_reference', (win, result, error)))
        threading.Thread(target=worker, daemon=True).start()

    def command_help_loaded(self, win, reference, error):
        if not win.winfo_exists():
            return
        win.reference = reference
        win.by_id = {entry['id']: entry for entry in reference['entries']}
        win.refresh.configure(state='normal')
        packs = '; '.join(f"{p['name']} — {p['status']} ({p['count']} entries)" for p in reference['packs'][:5])
        if len(reference['packs']) > 5:
            packs += f"; {len(reference['packs']) - 5} more packs (see mod entries)."
        win.summary.set((('Mod scan incomplete: ' + error + '\n') if error else '') +
                        f"{len(reference['entries'])} reference entries. " +
                        (packs or 'No additional installed mod commands found.') +
                        '\nCore syntax: /help <command> is authoritative. Mod discovery is best effort.' +
                        ('\nScan warnings: ' + '; '.join(reference['warnings'])[:400] if reference['warnings'] else ''))
        self._filter_command_help(win)

    def _filter_command_help(self, win):
        query, category = win.query.get().strip().casefold(), win.category.get()
        selected = win.tree.selection()
        win.tree.delete(*win.tree.get_children())
        for entry in win.reference['entries']:
            text = ' '.join(str(entry.get(k, '')) for k in ('title', 'syntax', 'summary', 'pack', 'status')).casefold()
            if (category == 'All commands' or entry['category'] == category) and (not query or query in text):
                win.tree.insert('', 'end', iid=entry['id'], text=entry['title'] +
                                (' · ' + entry['pack'] if entry.get('pack') else ''))
        items = win.tree.get_children()
        if items:
            win.tree.selection_set(selected[0] if selected and selected[0] in items else items[0])
        self._show_command_help_entry(win)

    def _show_command_help_entry(self, win):
        from command_help import entry_text
        selection = win.tree.selection()
        entry = win.by_id.get(selection[0]) if selection else None
        win.details.configure(state='normal')
        win.details.delete('1.0', 'end')
        win.details.insert('1.0', entry_text(entry) if entry else
                           'No matching commands. Try a shorter search or another category.\n\n'
                           'Dynamic mod commands need documentation from the pack author. Disabled packs are not available.')
        win.details.configure(state='disabled')

    def _copy_command_help(self, win, example):
        from command_help import entry_text
        selection = win.tree.selection()
        entry = win.by_id.get(selection[0]) if selection else None
        if entry:
            self.clipboard_clear()
            self.clipboard_append(entry.get('example', '') if example else entry_text(entry))

    def refresh_catalogue(self):
        if not hasattr(self, 'version_tree'):
            return self._refresh_catalogue_legacy()
        with self.preserve_tree(self.version_tree, 'version_tree'):
            self._refresh_catalogue_legacy()

    def refresh_history(self, keep_selection=False):
        if not self.history or not hasattr(self, 'hist_tree'):
            return
        with self.preserve_tree(self.hist_tree, 'hist_tree'), self.preserve_tree(self.ev_tree, 'ev_tree'):
            self._refresh_history_legacy(keep_selection)

    def refresh_backups(self):
        if not getattr(self, '_features_ready', False):
            return self._refresh_backups_legacy()
        records = experience.backup_catalogue(self.root_dir)
        term = self.backup_filter.get().casefold()
        kind = self.backup_kind_filter.get()
        with self.preserve_tree(self.backup_tree, 'backup_tree'):
            self.backup_tree.delete(*self.backup_tree.get_children())
            self._backups = {}
            for item in records:
                if kind != 'all' and item['kind'] != kind:
                    continue
                if term and term not in ' '.join([item['name'], item['label'], item['world']]).casefold():
                    continue
                identity = self.backup_tree.insert('', 'end', text=item['label'] or item['name'], values=(
                    item['world'], item['kind'], datetime.fromtimestamp(item['mtime']).strftime('%Y-%m-%d %H:%M'),
                    f"{item['bytes']/1048576:.1f} MB", item['verified'] or 'Not verified'))
                self._backups[identity] = item['path']
        self.backup_note.configure(text=f"{len(self._backups)} shown / {len(records)} archives • {sum(i['bytes'] for i in records)/1048576:.0f} MB total")
        matching = [i for i in records if i['world'] == self.level_name()]
        self.backup_health_line.configure(text='Latest backup: ' + (
            f"{datetime.fromtimestamp(matching[0]['mtime']):%b %d, %H:%M} • {matching[0]['kind']}" if matching else 'none for this world'))

    def label_selected_backup(self):
        path = self._selected_backup()
        if path:
            label = simpledialog.askstring('Backup label', 'Describe this backup:', parent=self)
            if label is not None:
                experience.label_backup(self.root_dir, path, label)
                self.refresh_backups()

    def backup_verify(self):
        path = self._selected_backup()
        if path:
            def check():
                self.report_stage(0, 0, 'Verifying archive checksums')
                experience.verify_catalogued(self.root_dir, path)
                return f'Verified: {path.name}'
            self._maintenance_task('Verify backup', check, readonly=True)

    def complete_backup(self):
        label = simpledialog.askstring('Complete restore point', 'Label (optional):', parent=self)
        if label is None:
            return
        def work():
            result = recovery.create_restore_point(self.root_dir, label, self.report_stage)
            return f'Complete restore point saved: {result.name}'
        self._maintenance_task('Complete restore point', work)

    def backup_restore(self):
        path = self._selected_backup()
        if not path:
            return
        import zipfile
        try:
            with zipfile.ZipFile(path) as archive:
                complete = 'restore-point.json' in archive.namelist()
        except (OSError, zipfile.BadZipFile) as exc:
            messagebox.showerror('Cannot read backup', str(exc), parent=self)
            return
        if not complete:
            return self._backup_restore_legacy()
        if not messagebox.askyesno('Restore complete point',
            f"Restore {path.name}, including its saved world, server files, settings and packs?\n\nThe archive is verified and a fresh recovery point is made first.", parent=self):
            return
        def work():
            safety = recovery.restore_point(self.root_dir, path, self.report_stage)
            return f'Restored complete point. Previous state saved in {safety.name}.'
        self._maintenance_task('Restore complete point', work)

    def report_stage(self, done, total, label):
        if getattr(self, '_task_cancel', None) and self._task_cancel.is_set() and not label.lower().startswith(('installing', 'verifying clean')):
            raise RuntimeError('Cancelled before applying changes.')
        stamp = time.monotonic()
        last = getattr(self, '_last_stage_update', (0, ''))
        if label != last[1] or stamp-last[0] >= .15 or done == total:
            self.q.put(('feature_progress', (done, total, label)))
            self._last_stage_update = (stamp, label)

    def cancel_preparation(self):
        if not self._maintenance and not getattr(self, '_update_busy', False):
            return
        if self._task_phase.lower().startswith('installing'):
            self.task_status.set('Applying changes must finish. Recovery remains available if an error occurs.')
            return
        self._task_cancel.set()
        self.task_status.set('Cancellation requested. Finishing the current safe step…')

    def recovery_dialog(self):
        pending = recovery.operations(self.root_dir, True)
        dialog, text = self.text_dialog('Interrupted operations', '\n\n'.join(
            f"{o['id']}\n{o.get('kind')} — {o.get('state')}" for o in pending) or 'No interrupted operations.')
        if not pending:
            return
        def restore():
            if not messagebox.askyesno('Recover', 'Restore the saved files from all listed interrupted operations?', parent=dialog):
                return
            dialog.destroy()
            def work():
                for operation in pending:
                    recovery.recover(self.root_dir, operation['id'])
                return 'Recovery completed. Review settings before starting.'
            self._maintenance_task('Recover interrupted changes', work)
        ttk.Button(dialog, text='Recover previous files', command=restore).pack(pady=12)

    def text_dialog(self, title, body):
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.geometry('780x570')
        dialog.transient(self)
        dialog.configure(bg=PANEL)
        frame = tk.Frame(dialog, bg=PANEL)
        frame.pack(fill='both', expand=True, padx=12, pady=12)
        text = tk.Text(frame, wrap='word', bg=INPUT, fg=FG, padx=16, pady=16,
                       font=('Consolas', 10), relief='flat')
        scroll = ttk.Scrollbar(frame, orient='vertical', command=text.yview)
        scroll.pack(side='right', fill='y')
        text.configure(yscrollcommand=scroll.set)
        text.pack(fill='both', expand=True)
        text.insert('1.0', body)
        text.configure(state='disabled')
        return dialog, text

    def save_props(self):
        current = self.props_path.read_text(encoding='utf-8-sig')
        if getattr(self, '_props_baseline', current) != current:
            messagebox.showwarning('Settings changed', 'Settings changed on disk. Reload before saving.', parent=self)
            return
        proposed = {k: v.get().strip() for k, v in self.prop_vars.items()}
        new, changes = experience.settings_preview(current, proposed)
        if not changes:
            self.props_note.configure(text='Nothing changed.')
            return
        dialog, _ = self.text_dialog('Review settings changes', 'Restart required for these changes:\n\n' + '\n'.join(
            f'{key}\n  {before or "(empty)"} → {after or "(empty)"}\n' for key, before, after in changes))
        def save():
            try:
                experience.save_settings(self.root_dir, current, new)
                dialog.destroy()
                self.load_props()
                self.props_note.configure(text='Saved. Restart the server to apply; undo is available from Settings.')
                self.max_players = self._read_max_players()
            except Exception as exc:
                messagebox.showerror('Settings were not saved', str(exc), parent=dialog)
        ttk.Button(dialog, text='Save these changes', command=save).pack(pady=(0, 12))

    def undo_props(self):
        if messagebox.askyesno('Undo settings', 'Undo the last matching settings change? A restart will be required.', parent=self):
            try:
                experience.undo_settings(self.root_dir)
                self.load_props()
            except Exception as exc:
                messagebox.showerror('Could not undo', str(exc), parent=self)

    def settings_history_dialog(self):
        entries = experience.settings_history(self.root_dir)
        body = '\n\n'.join(f"{item['created']} — {item['label']}\n" + '\n'.join(
            f'  {k}: {a} → {b}' for k, a, b in item['changes']) for item in entries)
        self.text_dialog('Settings history', body or 'No saved settings changes yet.')

    def maintenance_options(self):
        path = self.root_dir / 'manager_config.json'
        data = json.loads(path.read_text()) if path.exists() else {}
        dialog = tk.Toplevel(self)
        dialog.title('Maintenance scheduling')
        dialog.transient(self)
        wait = tk.BooleanVar(value=data.get('maintenance_wait_empty', False))
        maximum = tk.StringVar(value=str(data.get('maintenance_max_delay', 30)))
        ttk.Checkbutton(dialog, text='Wait for players to leave before scheduled restarts and updates', variable=wait).pack(padx=20, pady=20)
        ttk.Label(dialog, text='Maximum delay in minutes (0–1440):').pack(padx=20)
        ttk.Entry(dialog, textvariable=maximum, width=8).pack(pady=8)
        ttk.Label(dialog, text='After the limit, players receive a final one-minute warning.\nManual restarts keep their normal one-minute warning.').pack(padx=20, pady=12)
        def save():
            try:
                value = int(maximum.get())
                if not 0 <= value <= 1440:
                    raise ValueError('Use 0–1440 minutes.')
                fresh = json.loads(path.read_text()) if path.exists() else {}
                fresh.update(maintenance_wait_empty=wait.get(), maintenance_max_delay=value)
                atomic_json(path, fresh)
                dialog.destroy()
            except (ValueError, OSError) as exc:
                messagebox.showerror('Invalid maintenance settings', str(exc), parent=dialog)
        ttk.Button(dialog, text='Save', command=save).pack(pady=16)

    def dependencies_dialog(self):
        index, duplicates = mods.pack_index(self.root_dir)
        enabled = {str(e['pack_id']).lower() for entries in mods.active_packs(self.root_dir).values() for e in entries}
        issues = mods.dependency_issues(index, enabled) + [f'Duplicate UUID: {u}' for u in duplicates]
        self.text_dialog('Pack dependencies', '\n'.join(issues) or 'Enabled pack dependencies and versions match.\nScript API compatibility is tested by an update rehearsal.')

    def profiles_dialog(self):
        dialog = tk.Toplevel(self)
        dialog.title('Mod profiles')
        dialog.transient(self)
        dialog.geometry('550x320')
        ttk.Label(dialog, text='A profile saves which packs and versions this world enables.').pack(padx=20, pady=15)
        names = list(mods.profiles(self.root_dir))
        selected = tk.StringVar(value=names[0] if names else '')
        combo = ttk.Combobox(dialog, values=names, textvariable=selected, width=50)
        combo.pack(padx=20, pady=10)
        def save():
            try:
                if selected.get() in mods.profiles(self.root_dir) and not messagebox.askyesno('Replace profile', 'Replace this saved profile?', parent=dialog):
                    return
                mods.save_profile(self.root_dir, selected.get())
                combo.configure(values=list(mods.profiles(self.root_dir)))
            except Exception as exc:
                messagebox.showerror('Profile could not be saved', str(exc), parent=dialog)
        def compare(apply=False):
            try:
                changes = mods.compare_profile(self.root_dir, selected.get())
                index, _ = mods.pack_index(self.root_dir)
                name = lambda u: index.get(u, {}).get('name', u)
                text = 'Enable:\n' + '\n'.join(map(name, changes['enable'])) + '\n\nDisable:\n' + '\n'.join(map(name, changes['disable']))
                text += '\n\nProblems:\n' + ('\n'.join(changes['issues']) or 'None')
                if not apply:
                    self.text_dialog('Compare profile', text)
                elif not changes['issues'] and messagebox.askyesno('Apply profile', text + '\n\nCreate a restore point and apply?', parent=dialog):
                    profile = selected.get()
                    dialog.destroy()
                    def work():
                        path = mods.apply_profile(self.root_dir, profile, self.report_stage)
                        from build_admin_addon import refresh_generated
                        try:
                            note = refresh_generated(self.root_dir, lock_held=True, progress=self.report_stage)
                        except Exception as exc:
                            raise RuntimeError(f'Profile applied; recovery point {path.name}. Help/menu refresh failed: {exc}') from exc
                        return f'Profile applied. Recovery point: {path.name}. {note}'
                    self._maintenance_task('Apply mod profile', work)
                elif changes['issues']:
                    self.text_dialog('Profile needs attention', text)
            except Exception as exc:
                messagebox.showerror('Profile could not be read', str(exc), parent=dialog)
        ttk.Button(dialog, text='Save current selection as profile', command=save).pack(pady=8)
        ttk.Button(dialog, text='Compare with current world', command=compare).pack(pady=8)
        ttk.Button(dialog, text='Apply selected profile…', command=lambda: compare(True)).pack(pady=8)

    def incident_dialog(self):
        self.task_status.set('Building troubleshooting report…')
        def work():
            try:
                self.q.put(('feature_report', ('Troubleshooting report', experience.incident_report(self.root_dir))))
            except Exception as exc:
                self.q.put(('error', str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def show_exportable_report(self, title, body):
        dialog, _ = self.text_dialog(title, body)
        def export():
            destination = filedialog.asksaveasfilename(parent=dialog, title='Save local report',
                defaultextension='.txt', initialfile='bedrock-report.txt', filetypes=[('Text report', '*.txt')])
            if destination:
                atomic_text(Path(destination), body)
        ttk.Label(dialog, text='Review before sharing. Reports remain local until you share them.').pack(pady=4)
        ttk.Button(dialog, text='Save report…', command=export).pack(pady=(0, 12))

    def rehearse_update(self):
        version = self._pending_version()
        if not version:
            self.show_page('update')
            messagebox.showinfo('Choose a version', 'Select a server version on the Update page, then choose Tools → Rehearse selected update.', parent=self)
            return
        if not messagebox.askyesno('Rehearse update', f'Test {version} using a disposable copy of the current world?\n\nThe server must be stopped. Testing may take a few minutes.', parent=self):
            return
        def work():
            import bedrock_update
            from bedrock_rehearsal import rehearse
            archive = bedrock_update.download(version, self.root_dir, self.report_stage)
            report, path = rehearse(self.root_dir, archive, version, self.report_stage, self._task_cancel)
            self.q.put(('feature_report', ('Update rehearsal', json.dumps(report, indent=2))))
            return f"Rehearsal {'passed' if report['passed'] else 'needs attention'}: {path.name}"
        self._maintenance_task('Rehearse update', work)

    def feature_tick(self):
        self._samples.append((self.stats.cpu_percent if self.stats.found else None,
                              self.stats.mem_gb if self.stats.found else None))
        self.draw_trends()
        self._repeat('feature_tick', 3000, self.feature_tick)

    def draw_trends(self):
        canvas = self.trend_canvas
        canvas.delete('all')
        width, height = canvas.winfo_width(), max(142, canvas.winfo_height())
        for index, (title, color) in enumerate([('CPU', BLUE), ('MEMORY', PURPLE)]):
            top = index * height / 2
            left, right, baseline = 8, max(20, width-8), top+62
            canvas.create_text(left, top+12, text=title, fill=FG_DIM, anchor='w', font=('Segoe UI', 9))
            ceiling = 100 if index == 0 else max(1, self.stats.total_gb)
            values = [s[index] for s in self._samples]
            for y in (baseline, baseline-20):
                canvas.create_line(left, y, right, y, fill=LINE, dash=(2, 4))
            segments, segment = [], []
            for i, value in enumerate(values):
                if value is None:
                    if segment:
                        segments.append(segment)
                    segment = []
                else:
                    segment += [left+i/199*max(1, right-left), baseline-min(1, max(0, value)/ceiling)*32]
            if segment:
                segments.append(segment)
            for points in segments:
                if len(points) >= 4:
                    canvas.create_polygon(points[0], baseline, *points, points[-2], baseline,
                                           fill='#3a2551' if index else '#233148', outline='')
                    canvas.create_line(*points, fill=color, width=2)
            current = values[-1] if values else None
            if current is not None:
                value = f'{current:.1f}' + ('%' if index == 0 else ' GB')
                canvas.create_text(right, top+12, text=value, fill=color, anchor='e', font=('Segoe UI', 10, 'bold'))
            else:
                canvas.create_text(left, top+41, text='Waiting for live samples', fill=FG_FAINT,
                                   anchor='w', font=('Segoe UI', 8))
