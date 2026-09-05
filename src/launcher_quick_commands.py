"""Searchable admin action palette with typed fields and a read-only preview."""
import tkinter as tk
from tkinter import ttk

from admin_quick_commands import PRESETS, BY_ID, DEFAULT_FAVORITES, prepare, blocked_reason
from bedrock_storage import atomic_json
from launcher_theme import PANEL, INPUT, FG, FG_DIM, GREEN, AMBER, PORTAL


class QuickCommandWindow(tk.Toplevel):
    def __init__(self, app, preset_id=None, player=''):
        super().__init__(app)
        self.app = app
        self.title('FlintDock · Admin quick commands')
        self.geometry('920x690')
        self.minsize(780, 610)
        self.configure(bg=PANEL)
        self.selected_id = None
        self.query = tk.StringVar()
        self.category = tk.StringVar(value='All categories')
        self.favorites_only = tk.BooleanVar(value=False)
        saved = app.ui.get('admin_quick_favorites', list(DEFAULT_FAVORITES))
        self.favorites = set(item for item in saved if isinstance(item, str) and item in BY_ID) if isinstance(saved, list) else set(DEFAULT_FAVORITES)
        self.values = {key: tk.StringVar(value=player if key == 'player' and player in app.players else '')
                       for key in ('player', 'destination', 'message', 'reason')}
        self.values['reason'].set('Please rejoin after contacting an administrator.')
        self._players = None
        self._timer = None
        self.protocol('WM_DELETE_WINDOW', self.destroy)
        tk.Label(self, text='ADMIN QUICK COMMANDS', bg=PANEL, fg=PORTAL,
                 font=('Segoe UI', 16, 'bold'), anchor='w').pack(fill='x', padx=18, pady=(16, 4))
        tk.Label(self, text='Choose an action → pick players if needed → review the exact command → send',
                 bg=PANEL, fg=FG_DIM, anchor='w').pack(fill='x', padx=18)

        filters = tk.Frame(self, bg=PANEL)
        filters.pack(fill='x', padx=18, pady=12)
        ttk.Label(filters, text='Search').pack(side='left')
        search = ttk.Entry(filters, textvariable=self.query)
        search.pack(side='left', fill='x', expand=True, padx=8)
        ttk.Combobox(filters, textvariable=self.category, values=['All categories'] + list(dict.fromkeys(p.category for p in PRESETS)),
                     state='readonly', width=18).pack(side='left')
        ttk.Checkbutton(filters, text='Favorites only', variable=self.favorites_only).pack(side='left', padx=(10, 0))

        # Reserve the footer before allocating expandable content, including at minimum size.
        footer = tk.Frame(self, bg=PANEL)
        footer.pack(side='bottom', fill='x', padx=18, pady=(8, 14))
        self.result = tk.StringVar(value='Selecting or double-clicking an action never sends it.')
        result = tk.Label(footer, textvariable=self.result, bg=PANEL, fg=AMBER, anchor='w', justify='left', wraplength=860)
        result.pack(fill='x', pady=(0, 8))
        result.bind('<Configure>', lambda e: result.configure(wraplength=max(200, e.width)))
        buttons = tk.Frame(footer, bg=PANEL)
        buttons.pack(fill='x')
        self.favorite = ttk.Button(buttons, text='Add favorite', command=self.toggle_favorite)
        self.favorite.pack(side='left')
        ttk.Button(buttons, text='Copy command', command=self.copy_command).pack(side='left', padx=8)
        ttk.Button(buttons, text='Command help', command=app.command_help_dialog).pack(side='left')
        self.send_button = ttk.Button(buttons, text='Send command', command=self.send)
        self.send_button.pack(side='right')
        ttk.Button(buttons, text='Close', command=self.destroy).pack(side='right', padx=8)

        split = tk.PanedWindow(self, bg=PANEL, sashwidth=8, borderwidth=0)
        split.pack(fill='both', expand=True, padx=18)
        left, right = tk.Frame(split, bg=PANEL), tk.Frame(split, bg=PANEL)
        split.add(left, minsize=220, width=265)
        split.add(right, minsize=380)
        self.tree = ttk.Treeview(left, show='tree', selectmode='browse')
        scroll = ttk.Scrollbar(left, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.tree.pack(fill='both', expand=True)
        self.heading = tk.StringVar()
        tk.Label(right, textvariable=self.heading, bg=PANEL, fg=FG, font=('Segoe UI', 13, 'bold'),
                 anchor='w').pack(fill='x', pady=(0, 6))
        self.description = tk.StringVar()
        desc = tk.Label(right, textvariable=self.description, bg=PANEL, fg=FG_DIM, justify='left', anchor='w', wraplength=480)
        desc.pack(fill='x', pady=(0, 8))
        desc.bind('<Configure>', lambda e: desc.configure(wraplength=max(200, e.width)))
        self.inputs = tk.Frame(right, bg=PANEL)
        self.inputs.pack(fill='x')
        self.inputs.columnconfigure(1, weight=1)
        self.fields = {}
        for row, (key, title) in enumerate([('player', 'Player to affect'), ('destination', 'Destination player'), ('message', 'Message'), ('reason', 'Kick reason')]):
            label = ttk.Label(self.inputs, text=title)
            label.grid(row=row, column=0, sticky='w', padx=(0, 10), pady=4)
            widget = (ttk.Combobox(self.inputs, textvariable=self.values[key], state='readonly', width=18)
                      if key in ('player', 'destination') else ttk.Entry(self.inputs, textvariable=self.values[key], width=20))
            widget.grid(row=row, column=1, sticky='ew', pady=4)
            self.fields[key] = label, widget
        self.refresh_button = ttk.Button(right, text='Refresh online roster', command=self.refresh_roster)
        self.refresh_button.pack(anchor='w', pady=6)
        ttk.Label(right, text='EXACT COMMAND PREVIEW').pack(anchor='w', pady=(8, 4))
        self.preview = tk.Text(right, height=3, wrap='word', bg=INPUT, fg=FG, relief='flat', padx=10, pady=8,
                               state='disabled', font=('Consolas', 10), width=30)
        self.preview.pack(fill='both', expand=True)
        self.requirements = tk.StringVar()
        requirements = tk.Label(right, textvariable=self.requirements, bg=PANEL, fg=FG_DIM, justify='left', anchor='w', wraplength=480)
        requirements.pack(fill='x', pady=(8, 0))
        requirements.bind('<Configure>', lambda e: requirements.configure(wraplength=max(200, e.width)))
        self.live = tk.StringVar()
        live = tk.Label(right, textvariable=self.live, bg=PANEL, fg=AMBER, justify='left', anchor='w', wraplength=480)
        live.pack(fill='x', pady=(6, 0))
        live.bind('<Configure>', lambda e: live.configure(wraplength=max(200, e.width)))

        self.tree.bind('<<TreeviewSelect>>', self.select)
        self.query.trace_add('write', lambda *_: self.filter())
        self.category.trace_add('write', lambda *_: self.filter())
        self.favorites_only.trace_add('write', lambda *_: self.filter())
        for value in self.values.values():
            value.trace_add('write', lambda *_: self.update_preview())
        self.filter(preset_id)
        self.tick()
        search.focus_set()

    def filter(self, prefer=None):
        current = prefer or self.selected_id
        self.tree.delete(*self.tree.get_children())
        terms = self.query.get().casefold().split()
        for preset in PRESETS:
            text = f'{preset.label} {preset.category} {preset.description} {preset.command}'.casefold()
            if (all(term in text for term in terms) and
                    (self.category.get() == 'All categories' or preset.category == self.category.get()) and
                    (not self.favorites_only.get() or preset.id in self.favorites)):
                self.tree.insert('', 'end', iid=preset.id, text=('★ ' if preset.id in self.favorites else '') + preset.label)
        items = self.tree.get_children()
        if items:
            self.tree.selection_set(current if current in items else items[0])
            self.tree.see(current if current in items else items[0])
        self.select()

    def select(self, _event=None):
        selection = self.tree.selection()
        self.selected_id = selection[0] if selection else None
        preset = BY_ID.get(self.selected_id)
        self.heading.set(preset.label if preset else 'No matching quick commands')
        self.description.set(preset.description if preset else 'Try another search or clear Favorites only.')
        for key, widgets in self.fields.items():
            for widget in widgets:
                widget.grid() if preset and key in preset.fields else widget.grid_remove()
        self.favorite.configure(text='Remove favorite' if self.selected_id in self.favorites else 'Add favorite',
                                state='normal' if preset else 'disabled')
        self.requirements.set((('Python manager request.' if preset.manager else 'Server-console command. '
                               + ('Marked cheat-required by Bedrock; the server enforces its current settings.' if preset.cheats else 'Does not require enabling cheats.'))
                               + '\nNo cheats or permissions settings are changed by this panel.') if preset else '')
        self.send_button.configure(text='Review & send…' if preset and preset.confirm else 'Send command')
        self.update_preview()

    def get_values(self):
        return {key: value.get() for key, value in self.values.items()}

    def update_preview(self):
        try:
            text = prepare(self.selected_id, self.get_values())
            valid = True
        except ValueError as exc:
            text, valid = str(exc), False
        self.preview.configure(state='normal')
        self.preview.delete('1.0', 'end')
        self.preview.insert('1.0', text)
        self.preview.configure(state='disabled')
        blocked = blocked_reason(self.app)
        busy = getattr(self.app, '_quick_busy', False)
        self.send_button.configure(state='normal' if valid and not blocked and not busy else 'disabled')

    def tick(self):
        if self._timer is not None:
            self.after_cancel(self._timer)
            self._timer = None
        roster = tuple(sorted(self.app.players, key=str.casefold))
        if roster != self._players:
            self._players = roster
            for key in ('player', 'destination'):
                self.fields[key][1].configure(values=roster)
                if self.values[key].get() not in roster:
                    self.values[key].set('')  # Never silently retarget after someone leaves.
        blocked = blocked_reason(self.app)
        self.live.set('Sending request…' if getattr(self.app, '_quick_busy', False) else blocked or
                      f'{len(roster)} player(s) in roster. Replies and failures appear in Console.')
        self.refresh_button.configure(state='normal' if not blocked and not getattr(self.app, '_quick_busy', False) else 'disabled')
        self.update_preview()
        self._timer = self.after(700, self.tick)

    def send(self):
        self.app.run_admin_quick_command(self.selected_id, self.get_values())
        self.update_preview()

    def refresh_roster(self):
        self.app.run_admin_quick_command('list', {})

    def copy_command(self):
        try:
            command = prepare(self.selected_id, self.get_values())
        except ValueError as exc:
            self.result.set(str(exc))
            return
        self.clipboard_clear()
        self.clipboard_append(command)
        self.result.set('Copied only; nothing was sent. Recheck the player and world before using elsewhere.')

    def toggle_favorite(self):
        if not self.selected_id:
            return
        previous = set(self.favorites)
        self.favorites.symmetric_difference_update({self.selected_id})
        old = self.app.ui.get('admin_quick_favorites')
        self.app.ui['admin_quick_favorites'] = sorted(self.favorites)
        try:
            atomic_json(self.app.root_dir / 'launcher_ui.json', self.app.ui)
        except OSError as exc:
            self.favorites = previous
            if old is None:
                self.app.ui.pop('admin_quick_favorites', None)
            else:
                self.app.ui['admin_quick_favorites'] = old
            self.result.set(f'Could not save favorites: {exc}')
        self.filter()

    def destroy(self):
        if self._timer is not None:
            self.after_cancel(self._timer)
            self._timer = None
        super().destroy()
