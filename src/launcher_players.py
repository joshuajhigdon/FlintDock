"""Unified player directory: history, queued commands and persistent roles."""
import json
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from bedrock_runtime import rpc
from bedrock_storage import operation_lock
from launcher_theme import PANEL, INPUT, FG, FG_DIM, GREEN, AMBER, PORTAL
from player_history import KIND_LABEL, render_queue_command
from player_permissions import ROLES, directory_snapshot, set_player_role, valid_xuid


class PlayerDirectory(tk.Frame):
    def __init__(self, app, parent):
        super().__init__(parent, bg=PANEL)
        self.app = app
        self.people = {}
        self.revision = None
        self._last_refresh = 0
        self._picked = None
        self._busy = False
        self.query = tk.StringVar()
        self.status = tk.StringVar(value='All players')
        self.count = tk.StringVar()
        self.feedback = tk.StringVar()
        self.summary = tk.StringVar(value='Select a player to view activity, queue commands or adjust their role.')
        self.role = tk.StringVar(value='member')
        self.role_note = tk.StringVar()
        self.command = tk.StringVar()
        self.preview = tk.StringVar(value='Choose a player and enter a command.')

        toolbar = tk.Frame(self, bg=PANEL)
        toolbar.pack(fill='x', pady=(0, 6))
        ttk.Label(toolbar, text='Find player').pack(side='left')
        self.search = ttk.Entry(toolbar, textvariable=self.query)
        self.search.pack(side='left', fill='x', expand=True, padx=8)
        ttk.Combobox(toolbar, textvariable=self.status, state='readonly', width=14,
                     values=('All players', 'Online', 'Offline')).pack(side='left')
        ttk.Button(toolbar, text='Refresh', command=self.refresh).pack(side='left', padx=(8, 0))
        self.label(self, self.count, FG_DIM).pack(fill='x', pady=(0, 5))
        self.label(self, self.feedback, AMBER).pack(side='bottom', fill='x', pady=(5, 0))

        area = tk.Frame(self, bg=PANEL)
        area.pack(fill='both', expand=True)
        area.columnconfigure(0, weight=1)
        area.rowconfigure(0, weight=1, minsize=100)
        area.rowconfigure(2, weight=2, minsize=175)
        table = tk.Frame(area, bg=PANEL)
        table.grid(row=0, column=0, sticky='nsew')
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(table, columns=('status', 'role', 'seen', 'queue'),
                                 show='tree headings', selectmode='browse', height=5)
        for key, title, width in (('#0', 'PLAYER', 160), ('status', 'STATUS', 65),
                                  ('role', 'SAVED ROLE', 90), ('seen', 'LAST SEEN', 142),
                                  ('queue', 'QUEUED', 62)):
            self.tree.heading(key, text=title, command=lambda k=key: self.sort(k))
            self.tree.column(key, width=width, minwidth=60, stretch=key in ('#0', 'seen'))
        self.tree.grid(row=0, column=0, sticky='nsew')
        sy = ttk.Scrollbar(table, orient='vertical', command=self.tree.yview)
        sy.grid(row=0, column=1, sticky='ns')
        sx = ttk.Scrollbar(table, orient='horizontal', command=self.tree.xview)
        sx.grid(row=1, column=0, sticky='ew')
        self.tree.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        self.tree.tag_configure('online', foreground=GREEN)
        self.tree.tag_configure('offline', foreground=FG_DIM)
        self.tree.bind('<<TreeviewSelect>>', self.pick)
        self.tree.bind('<Double-1>', lambda _e: self.tabs.select(self.activity_tab))
        self.label(area, self.summary, FG).grid(row=1, column=0, sticky='ew', pady=7)

        self.tabs = ttk.Notebook(area)
        self.tabs.grid(row=2, column=0, sticky='nsew')
        self.activity_tab = tk.Frame(self.tabs, bg=PANEL)
        self.queue_tab = tk.Frame(self.tabs, bg=PANEL)
        self.role_tab = tk.Frame(self.tabs, bg=PANEL)
        for frame, title in ((self.activity_tab, 'Recent activity'), (self.queue_tab, 'Command queue'),
                             (self.role_tab, 'Role & actions')):
            self.tabs.add(frame, text=title)

        self.events = self.table(self.activity_tab, ('when', 'event', 'detail'),
                                 (('WHEN', 137), ('EVENT', 65), ('DETAIL', 310)))
        ttk.Button(self.activity_tab, text='Open full activity / export', command=self.open_activity).pack(anchor='e', pady=3)

        self.label(self.queue_tab, 'Runs on their next join, even if FlintDock is closed while its manager stays running.', FG_DIM).pack(fill='x')
        self.label(self.queue_tab, 'Use {player} or a standalone @s as the target. Other selectors keep their normal scope.', FG_DIM).pack(fill='x')
        entry_row = tk.Frame(self.queue_tab, bg=PANEL)
        entry_row.pack(fill='x', pady=5)
        self.command_entry = ttk.Entry(entry_row, textvariable=self.command)
        self.command_entry.pack(side='left', fill='x', expand=True)
        self.queue_button = ttk.Button(entry_row, text='Queue for next join', command=self.add_queue)
        self.queue_button.pack(side='left', padx=(8, 0))
        self.label(self.queue_tab, self.preview, PORTAL).pack(fill='x')
        self.cancel_button = ttk.Button(self.queue_tab, text='Cancel selected pending command', command=self.cancel_queue)
        self.cancel_button.pack(side='bottom', anchor='e', pady=3)
        self.queue_tree = self.table(self.queue_tab, ('status', 'command'), (('STATUS', 130), ('COMMAND', 340)), height=2)

        self.label(self.role_tab, self.role_note, FG_DIM).pack(fill='x', pady=(8, 6))
        roles = tk.Frame(self.role_tab, bg=PANEL)
        roles.pack(fill='x')
        ttk.Label(roles, text='Player role').pack(side='left', padx=(0, 8))
        self.role_select = ttk.Combobox(roles, textvariable=self.role, values=ROLES, state='readonly', width=12)
        self.role_select.pack(side='left')
        self.role_button = ttk.Button(roles, text='Save role…', command=self.save_role)
        self.role_button.pack(side='left', padx=8)
        self.label(self.role_tab, 'Visitor: limited interaction • Member: normal play • Operator: broad admin control.', FG_DIM).pack(fill='x', pady=6)
        self.label(self.role_tab, 'Only this Xbox ID is changed. Other players and the server’s default role are left alone.', FG_DIM).pack(fill='x')
        actions = tk.Frame(self.role_tab, bg=PANEL)
        actions.pack(fill='x', pady=8)
        self.quick_button = ttk.Button(actions, text='Online quick commands…', command=self.quick_commands)
        self.quick_button.pack(side='left')
        ttk.Button(actions, text='Command help', command=app.command_help_dialog).pack(side='left', padx=8)
        ttk.Button(actions, text='Add to allowlist', command=self.allowlist).pack(side='left')
        self.query.trace_add('write', lambda *_: self.filter())
        self.status.trace_add('write', lambda *_: self.filter())
        self.command.trace_add('write', lambda *_: self.update_preview())
        self.refresh()
        app._repeat('player_directory_tick', 2000, self.tick)

    @staticmethod
    def label(parent, text, colour):
        kwargs = {'textvariable': text} if isinstance(text, tk.Variable) else {'text': text}
        label = tk.Label(parent, bg=PANEL, fg=colour, anchor='w', justify='left',
                         font=('Segoe UI', 9), wraplength=620, **kwargs)
        label.bind('<Configure>', lambda e: label.configure(wraplength=max(160, e.width)))
        return label

    @staticmethod
    def table(parent, columns, headings, height=3):
        frame = tk.Frame(parent, bg=PANEL)
        frame.pack(fill='both', expand=True, pady=(5, 0))
        tree = ttk.Treeview(frame, columns=columns, show='headings', selectmode='browse', height=height)
        for key, (title, width) in zip(columns, headings):
            tree.heading(key, text=title)
            tree.column(key, width=width, minwidth=60)
        bar = ttk.Scrollbar(frame, command=tree.yview)
        bar.pack(side='right', fill='y')
        tree.configure(yscrollcommand=bar.set)
        tree.pack(fill='both', expand=True)
        return tree

    def selected(self):
        selection = self.tree.selection()
        return selection[0] if selection and selection[0] in self.people else None

    def tick(self):
        if self.app.current_page == 'players':
            self.refresh()
        self.app._repeat('player_directory_tick', 2000, self.tick)

    def refresh(self, force=True):
        if not force and time.monotonic() - self._last_refresh < 1.5:
            return
        self._last_refresh = time.monotonic()
        try:
            snapshot = directory_snapshot(self.app.root_dir, self.app.history, self.app.players)
            self.people = {p['name']: p for p in snapshot['players']}
            self.revision = snapshot['revision']
            if snapshot['warnings']:
                self.feedback.set(' • '.join(snapshot['warnings']))
            elif not self.app.history:
                self.feedback.set('History is unavailable. Offline history and command queuing are disabled.')
            self.filter()
        except Exception as exc:
            self.feedback.set(f'Could not refresh the player directory: {exc}')

    def filter(self):
        name = self.selected()
        query, status = self.query.get().casefold().strip(), self.status.get()
        shown = []
        for person in self.people.values():
            if query and query not in person['name'].casefold():
                continue
            if status != 'All players' and person['online'] != (status == 'Online'):
                continue
            shown.append(person['name'])
        visible = set(shown)
        for iid in self.tree.get_children():
            if iid not in visible:
                self.tree.delete(iid)
        for index, iid in enumerate(shown):
            p = self.people[iid]
            values = ('Online' if p['online'] else 'Offline', p['role'].title(), p['last_seen'], p['queued'])
            if self.tree.exists(iid):
                if self.tree.item(iid, 'values') != tuple(map(str, values)):
                    self.tree.item(iid, values=values, tags=('online' if p['online'] else 'offline',))
            else:
                self.tree.insert('', 'end', iid=iid, text=iid, values=values,
                                 tags=('online' if p['online'] else 'offline',))
            self.tree.move(iid, '', index)
        if hasattr(self, '_sort'):
            self.sort(self._sort[0], toggle=False)
        if name in shown:
            if self.tree.selection() != (name,):
                self.tree.selection_set(name)
        elif name:
            self.tree.selection_remove(*self.tree.selection())
        online = sum(p['online'] for p in self.people.values())
        self.count.set(f'{online} online • {len(self.people) - online} offline • {len(shown)} shown'
                       + (' — No players yet. They appear after joining or being allowlisted.' if not self.people else ''))
        self.pick()

    def sort(self, column, toggle=True):
        previous = getattr(self, '_sort', (None, False))
        reverse = not previous[1] if toggle and previous[0] == column else previous[1] if not toggle else False
        def key(iid):
            p = self.people[iid]
            return {'#0': iid.casefold(), 'status': not p['online'], 'role': p['role'],
                    'seen': p['last_seen'], 'queue': p['queued']}[column]
        for i, iid in enumerate(sorted(self.tree.get_children(), key=key, reverse=reverse)):
            self.tree.move(iid, '', i)
        self._sort = (column, reverse)

    def pick(self, _event=None):
        name = self.selected()
        changed = name != self._picked
        self._picked = name
        person = self.people.get(name, {})
        if not name:
            self.summary.set('Select a player to view activity, queue commands or adjust their role.')
            self.role_note.set('Select a player first.')
        else:
            state = 'Online now' if person['online'] else 'Offline'
            self.summary.set(f"{name} • {state} • Playtime {person['playtime']} • {person.get('sessions', 0)} sessions")
            self.role_note.set(f"Saved role: {person['role'].title()} ({person['role_source']}). "
                               + ('Live changes request a permission reload; check Console or reconnect to verify.'
                                  if valid_xuid(person.get('xuid')) else 'No Xbox ID recorded—this player must join once first.'))
            if changed:
                self.role.set(person['role'] if person['role'] in ROLES else 'member')
        can_role = bool(name and valid_xuid(person.get('xuid')) and self.revision is not None and not self._busy)
        self.role_button.configure(state='normal' if can_role else 'disabled')
        self.quick_button.configure(state='normal' if name in self.app.players else 'disabled')
        self.update_preview()
        self.refresh_details()

    def refresh_details(self):
        name = self.selected()
        history = self.app.history
        try:
            events = history.timeline(player=name, limit=100) if history and name else []
            queued = history.queue_for(name, include_done=True)[-100:] if history and name else []
            self.sync_table(self.events, [(str(e['id']), (e['ts'], KIND_LABEL.get(e['kind'], e['kind']), e['detail'])) for e in events])
            def queue_status(q):
                if q['status'] != 'pending':
                    return 'Sent (unverified)'
                if q.get('xuid') and q['xuid'] != self.people.get(name, {}).get('xuid'):
                    return 'Held: Xbox ID changed'
                return 'Pending' if q.get('xuid') else 'Pending (name-based)'
            self.sync_table(self.queue_tree, [(str(q['id']), (queue_status(q), q['command'])) for q in queued])
        except Exception as exc:
            self.feedback.set(f'Player history could not be read: {exc}')

    @staticmethod
    def sync_table(tree, rows):
        current = set(tree.get_children())
        wanted = {iid for iid, _ in rows}
        for iid in current - wanted:
            tree.delete(iid)
        for i, (iid, values) in enumerate(rows):
            if iid not in current:
                tree.insert('', 'end', iid=iid, values=values)
            elif tree.item(iid, 'values') != tuple(map(str, values)):
                tree.item(iid, values=values)
            tree.move(iid, '', i)

    def update_preview(self):
        try:
            if not self.selected():
                raise ValueError('Select a player first.')
            command = render_queue_command(self.selected(), self.command.get())
            self.preview.set('Preview: ' + command)
            enabled = bool(self.app.history)
        except ValueError as exc:
            self.preview.set(str(exc))
            enabled = False
        self.queue_button.configure(state='normal' if enabled else 'disabled')

    def add_queue(self):
        name = self.selected()
        if not name or not self.app.history:
            return
        if self.enqueue(name, self.command.get()):
            self.command.set('')
            self.refresh()

    def enqueue(self, name, command):
        try:
            if self.app.manager.running() and self.app.manager.state.get('player_queue_protocol') != 2:
                raise ValueError('Restart the server using this updated launcher before adding queued commands.')
            preview = render_queue_command(name, command)
            if not messagebox.askyesno('Queue command',
                    f'Queue for {name} on their NEXT join?\n\n{preview}\n\n'
                    'This runs with server-console authority. Selectors such as @a can affect other players. '
                    'Sent means delivered, not confirmed successful.', parent=self):
                return False
            self.app.history.queue_add(name, command)
            self.feedback.set(f'Queued for {name} on their next join. You can cancel it below.')
            return True
        except Exception as exc:
            self.feedback.set(str(exc))
            return False

    def cancel_queue(self):
        selected, name = self.queue_tree.selection(), self.selected()
        if not selected or not name or not self.app.history:
            return
        if not messagebox.askyesno('Cancel queued command', 'Cancel this pending command? Already-sent commands cannot be undone.', parent=self):
            return
        try:
            removed = self.app.history.queue_delete(int(selected[0]), player=name)
            self.feedback.set('Pending command cancelled.' if removed else 'This command is no longer pending. Nothing was undone.')
            self.refresh()
        except Exception as exc:
            self.feedback.set(str(exc))

    def open_activity(self):
        name = self.selected()
        if not name or not self.app.history:
            return
        self.app.hist_player = name
        self.app.hist_scope.set('one')
        self.app.scope_btn.set_text(name)
        self.app.show_page('history')
        self.app.refresh_queue()

    def quick_commands(self):
        name = self.selected()
        if name in self.app.players:
            self.app.open_admin_quick_commands(player=name)

    def allowlist(self):
        name = self.selected()
        if not name:
            return
        if not self.app.server_up:
            self.feedback.set('Start the server before sending an allowlist command.')
            return
        self.app.send_command('allowlist add ' + json.dumps(name, ensure_ascii=False))

    def save_role(self):
        name = self.selected()
        person = self.people.get(name, {})
        role, revision, xuid = self.role.get(), self.revision, person.get('xuid')
        if self._busy or not name or not valid_xuid(xuid) or revision is None:
            return
        if not messagebox.askyesno('Change player role',
                f"Set {name} to {role.upper()}?\n\nOnly this player's Xbox ID will be changed. "
                'Operator grants broad administrative control. A copy of the previous permissions is kept. '
                'For a running server, check Console after the reload; reconnect/restart may be needed.', parent=self):
            return
        self._busy = True
        self.role_button.configure(state='disabled')
        self.feedback.set(f'Saving role for {name}…')
        def work():
            try:
                if self.app._maintenance or self.app._stopping_on_purpose or self.app._install_stage:
                    raise RuntimeError('Wait for the current server operation to finish.')
                if self.app.manager.running():
                    result = rpc(self.app.root_dir, 'player_role', xuid=xuid, role=role, revision=revision)
                else:
                    import bedrock_update
                    with operation_lock(self.app.root_dir):
                        if bedrock_update.server_running(self.app.root_dir):
                            raise RuntimeError('This server is running outside FlintDock. Stop it or attach its manager first.')
                        result = set_player_role(self.app.root_dir, xuid, role, revision)
                        result['reload_sent'] = False
                message = f'{name}: saved as {role}. '
                message += ('Permission reload sent—check Console to verify.' if result['reload_sent']
                            else 'Takes effect on the next server start/reload.')
                if self.app.history:
                    try:
                        self.app.history.record(name, 'command', f'Role override saved: {role}; live application not confirmed.')
                    except Exception:
                        message += ' History logging was unavailable.'
            except Exception as exc:
                message = 'Role save was not confirmed. Refresh and check the saved role before retrying. ' + str(exc)
                if 'Unknown manager operation' in message:
                    message = 'Restart the server using this updated launcher before changing roles while it is running.'
            self.app.q.put(('player_role_done', message))
        threading.Thread(target=work, daemon=True).start()

    def role_done(self, message):
        self._busy = False
        self.feedback.set(message)
        self.refresh()
