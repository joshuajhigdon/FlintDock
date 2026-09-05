"""Non-blocking launcher-update controls, separate from Bedrock server updates."""
from datetime import datetime
import os
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
import webbrowser

import flintdock_updates as updates
from app_paths import VERSION
from bedrock_storage import atomic_json
from launcher_theme import PANEL, FG, FG_DIM, PORTAL, AMBER


class AppUpdateController:
    def __init__(self, app, root=None, background=True, http=None):
        self.app = app
        self.root = Path(root) if root is not None else updates.storage_dir()
        self.background = background
        self.http = http
        self.events = queue.Queue()
        self.cancel_event = threading.Event()
        self.busy = False
        self.closed = False
        self.operation_auto = False
        self.candidate = None
        self.ready = None
        self.window = None
        self.status = 'Checks stable GitHub releases. Nothing is installed automatically.'
        try:
            self.settings = updates.read_settings(self.root)
        except updates.UpdateError as exc:
            self.settings = {**updates.DEFAULTS, 'check_enabled': False}
            self.status = str(exc)
        self.state = updates.read_state(self.root)
        self._next_poll = time.monotonic() + 30
        app._repeat('flintdock_updates', 500, self.tick)

    def tick(self):
        if self.closed:
            return
        for _ in range(100):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            self.handle(kind, value)
        if self.background and not self.busy and time.monotonic() >= self._next_poll:
            self._next_poll = time.monotonic() + 30
            try:
                self.settings = updates.read_settings(self.root)
                self.state = updates.read_state(self.root)
                if updates.check_due(self.settings, self.state, time.time()):
                    self.check(manual=False)
            except updates.UpdateError as exc:
                self.status = str(exc)
                self.refresh_window()
        self.app._repeat('flintdock_updates', 250 if self.busy else 1000, self.tick)

    def persist_state(self):
        atomic_json(updates.safe_local(self.root / 'state.json'), self.state)

    def check(self, manual=True):
        if self.busy or self.closed or (not manual and not self.settings['check_enabled']):
            return False
        try:
            self.state['last_attempt'] = time.time()
            self.persist_state()
        except (OSError, updates.UpdateError) as exc:
            self.status = f'Cannot save update-check status: {exc}'
            self.refresh_window()
            return False
        self.busy, self.operation_auto = True, not manual
        self.cancel_event.clear()
        self.status = 'Checking GitHub for a newer FlintDock release…'
        self.refresh_window()
        def work():
            try:
                release = updates.find_update(self.http, self.cancel_event)
                updates.check_cancel(self.cancel_event)
                self.events.put(('checked', release))
            except Exception as exc:
                self.events.put(('error', str(exc)))
        threading.Thread(target=work, daemon=True).start()
        return True

    def download(self, manual=True):
        if self.busy or self.closed or not self.candidate:
            return False
        if not manual and not (self.settings['auto_download'] and self.settings['check_enabled']):
            return False
        release = self.candidate
        self.busy, self.operation_auto = True, not manual
        self.cancel_event.clear()
        self.status = f'Downloading FlintDock {release.version}…'
        self.refresh_window()
        def work():
            last = 0
            def progress(done, total):
                nonlocal last
                now = time.monotonic()
                if now - last > .2 or done == total:
                    last = now
                    self.events.put(('progress', (done, total)))
            try:
                path = updates.download_update(release, self.root, self.http, self.cancel_event, progress)
                self.events.put(('downloaded', (release.version, path)))
            except Exception as exc:
                self.events.put(('error', str(exc)))
        threading.Thread(target=work, daemon=True).start()
        return True

    def handle(self, kind, value):
        if self.closed:
            return
        if kind == 'progress':
            done, total = value
            self.status = f'Downloading update: {done / 1024**2:.1f} / {total / 1024**2:.1f} MB'
        elif kind == 'error':
            self.busy = False
            self.status = str(value)[:500]
        elif kind == 'checked':
            self.busy = False
            self.candidate = value
            if value is None:
                self.status = f'FlintDock {VERSION} is up to date with the latest published stable release.'
            else:
                self.status = f'FlintDock {value.version} is available. Installed version: {VERSION}.'
                self.announce(value.version, 'available', self.status + ' Open Settings → FlintDock updates.')
                # Recheck current preferences after network work. Turning auto-download
                # off while a check is running must never start a download afterwards.
                if not self.cancel_event.is_set() and self.settings['auto_download'] and self.settings['check_enabled']:
                    self.download(manual=False)
        elif kind == 'downloaded':
            self.busy = False
            version, self.ready = value
            self.status = f'FlintDock {version} downloaded and SHA-256 verified. Ready for you to install; your server is unchanged.'
            self.announce(version, 'downloaded', self.status)
        self.refresh_window()

    def announce(self, version, state, message):
        self.app.task_status.set(message)
        key = f'notified_{state}'
        if self.state.get(key) != version:
            self.app.notify('info', f'FlintDock {version} {state}', message, 'launcher-update')
            self.state[key] = version
            try:
                self.persist_state()
            except OSError:
                pass

    def change_settings(self, settings):
        updates.save_settings(self.root, settings)
        old = self.settings
        self.settings = dict(settings)
        if self.busy and self.operation_auto and (not settings['check_enabled'] or not settings['auto_download']):
            self.cancel_event.set()
        self._next_poll = time.monotonic()
        if settings['check_enabled'] and settings['auto_download'] and not old['auto_download'] and not self.busy:
            self.check(manual=False)

    def close(self):
        self.closed = True
        self.cancel_event.set()

    def open_folder(self):
        try:
            folder = updates.safe_local(self.root / 'downloads')
            if not folder.is_dir():
                raise updates.UpdateError('No update has been downloaded yet.')
            os.startfile(str(folder))
        except (OSError, updates.UpdateError) as exc:
            self.status = str(exc)
            self.refresh_window()

    def show(self):
        if self.window is not None and self.window.winfo_exists():
            self.window.lift()
            return self.window
        win = self.window = tk.Toplevel(self.app)
        win.title('FlintDock · Launcher updates')
        win.configure(bg=PANEL)
        win.geometry('720x660')
        win.minsize(640, 660)
        from portal_art import apply_window_icon
        apply_window_icon(win)
        body = tk.Frame(win, bg=PANEL)
        body.pack(fill='both', expand=True, padx=20, pady=16)
        def label(text, colour=FG_DIM, font=('Segoe UI', 10)):
            widget = tk.Label(body, text=text, bg=PANEL, fg=colour, font=font,
                              anchor='w', justify='left', wraplength=640)
            widget.pack(fill='x', pady=(0, 9))
            widget.bind('<Configure>', lambda e: widget.configure(wraplength=max(200, e.width)))
            return widget
        label('FLINTDOCK UPDATES', PORTAL, ('Segoe UI', 18, 'bold'))
        label(f'Installed: {VERSION}  •  Windows x64  •  Stable releases', FG)
        label('These settings update the launcher, not Minecraft Bedrock or your add-ons.')
        win.checks = tk.BooleanVar(value=self.settings['check_enabled'])
        win.automatic = tk.BooleanVar(value=self.settings['auto_download'])
        win.interval = tk.StringVar(value=str(self.settings['interval_hours']))
        def save():
            try:
                self.change_settings({'check_enabled': win.checks.get(), 'auto_download': win.automatic.get(),
                                      'interval_hours': int(win.interval.get())})
                self.refresh_window()
            except (ValueError, OSError, updates.UpdateError) as exc:
                messagebox.showerror('Update preferences were not saved', str(exc), parent=win)
                win.checks.set(self.settings['check_enabled'])
                win.automatic.set(self.settings['auto_download'])
                win.interval.set(str(self.settings['interval_hours']))
        ttk.Checkbutton(body, text='Check GitHub periodically while FlintDock is open', variable=win.checks,
                        command=save).pack(anchor='w', pady=(0, 8))
        row = tk.Frame(body, bg=PANEL)
        row.pack(fill='x', pady=(0, 12))
        ttk.Label(row, text='Check every').pack(side='left')
        interval = ttk.Combobox(row, textvariable=win.interval, state='readonly', values=('1', '6', '12', '24'), width=5)
        interval.pack(side='left', padx=8)
        interval.bind('<<ComboboxSelected>>', lambda _e: save())
        ttk.Label(row, text='hours').pack(side='left')
        ttk.Checkbutton(body, text='Automatic updates: download new releases (do not install)', variable=win.automatic,
                        command=save).pack(anchor='w', pady=(0, 12))
        label('Downloads are checked against SHA-256 and kept in a separate update cache. '
              'Nothing is launched, extracted or replaced automatically. No server shutdown is triggered.')
        status_frame = tk.Frame(body, bg=PANEL)
        status_frame.pack(fill='x', pady=(0, 9))
        win.status_text = tk.Text(status_frame, height=3, wrap='word', font=('Segoe UI', 10),
                                  bg=PANEL, fg=AMBER, relief='flat', highlightthickness=0)
        status_scroll = ttk.Scrollbar(status_frame, command=win.status_text.yview)
        status_scroll.pack(side='right', fill='y')
        win.status_text.configure(yscrollcommand=status_scroll.set, state='disabled')
        win.status_text.pack(fill='x', expand=True)
        win.last_text = label('')
        buttons = tk.Frame(body, bg=PANEL)
        buttons.pack(fill='x', pady=4)
        win.check_button = ttk.Button(buttons, text='Check now', command=self.check)
        win.check_button.pack(side='left')
        win.download_button = ttk.Button(buttons, text='Download update', command=self.download)
        win.download_button.pack(side='left', padx=8)
        win.cancel_button = ttk.Button(buttons, text='Cancel', command=self.cancel_event.set)
        win.cancel_button.pack(side='left')
        links = tk.Frame(body, bg=PANEL)
        links.pack(fill='x', pady=8)
        ttk.Button(links, text='Open downloads folder', command=self.open_folder).pack(side='left')
        ttk.Button(links, text='GitHub releases', command=lambda: webbrowser.open(updates.RELEASES_URL)).pack(side='left', padx=8)
        label('To install a downloaded ZIP: stop the server/manager, close FlintDock, extract the entire ZIP '
              'into a NEW application folder outside your server folder, then open its FlintDock.exe. '
              'Keep the worker and _internal together. Your saved server selection is retained.')
        label('Free launcher updates are downloaded directly from GitHub. No GitHub or LootLabs account is required. '
              'Checksums do not replace publisher signing or antivirus checks.', font=('Segoe UI', 9))
        self.refresh_window()
        return win

    def refresh_window(self):
        win = self.window
        if win is None or not win.winfo_exists():
            return
        win.status_text.configure(state='normal')
        win.status_text.delete('1.0', 'end')
        win.status_text.insert('1.0', self.status)
        win.status_text.configure(state='disabled')
        try:
            stamp = float(self.state.get('last_attempt', 0))
            last = datetime.fromtimestamp(stamp).strftime('%Y-%m-%d %H:%M') if stamp else 'Not checked yet'
        except (ValueError, TypeError, OverflowError, OSError):
            last = 'Not checked yet'
        win.last_text.configure(text=f'Last attempted check: {last}  •  Source: {updates.REPOSITORY}')
        win.check_button.configure(state='disabled' if self.busy else 'normal')
        win.download_button.configure(state='normal' if self.candidate and not self.busy else 'disabled')
        win.cancel_button.configure(state='normal' if self.busy else 'disabled')
