"""Customer-controlled first-run setup. No bundled server, world, or credentials."""
from pathlib import Path
import json
import os
import queue
import re
import shutil
import tempfile
import threading
import webbrowser
import zipfile
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from app_paths import APP_ROOT, CODE_ROOT, VERSION, PRODUCT_NAME
from launcher_theme import BG, PANEL, CARD, INPUT, LINE, FG, FG_DIM, FG_FAINT, PORTAL, IGNITION, IGNITION_HOVER, SELECTED, HOVER
from portal_art import portal_scene, draw_shapes, apply_window_icon
from bedrock_storage import atomic_json, atomic_text, extract_zip, zip_members, world_path

DOWNLOAD = 'https://www.minecraft.net/en-us/download/server/bedrock'
EULA = 'https://www.minecraft.net/en-us/eula'


def preferences_path():
    # Stable data identity: rebranding must not lose an existing customer's server selection.
    return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'BedrockServerLauncher' / 'setup.json'


def data_location(path):
    raw = Path(path).absolute()
    if any(p.is_symlink() or getattr(p, 'is_junction', lambda: False)() for p in (raw, *raw.parents)):
        raise ValueError('Choose a local folder without symbolic links or junctions.')
    root = raw.resolve()
    if root == Path(root.anchor) or root == Path.home().resolve():
        raise ValueError('Choose a dedicated subfolder for this server.')
    if any(root == base or root.is_relative_to(base) or base.is_relative_to(root)
           for base in (APP_ROOT, CODE_ROOT)):
        raise ValueError('Keep server data outside the application installation folder.')
    if str(root).startswith('\\\\'):
        raise ValueError('Use a local drive, not a network share, for reliable world storage.')
    return root


def validate_server(path):
    root = data_location(path)
    for name in ('server.properties', 'bedrock_server.exe'):
        if not (root / name).is_file():
            raise ValueError(f'{name} was not found. Select the folder containing the Windows Bedrock dedicated server.')
    world_path(root)  # Reject unsafe level-name values before the launcher opens.
    return root


def install_server_archive(archive, destination, accepted=False):
    """Import a customer-selected official ZIP into an EMPTY folder, transactionally."""
    if not accepted:
        raise ValueError('Read and accept the Minecraft EULA on your own behalf before installing its server.')
    root = data_location(destination)
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ValueError('New server setup requires an empty folder. Use Connect existing server for an existing world.')
    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.bedrock-setup-', dir=root.parent) as tmp:
        stage = Path(tmp) / 'server'
        stage.mkdir()
        with zipfile.ZipFile(archive) as package:
            members = zip_members(package, stage, max_bytes=4*1024**3, max_files=100000)
            names = {m.filename.replace('\\', '/').lower() for m in members}
            if not {'bedrock_server.exe', 'server.properties'}.issubset(names):
                raise ValueError('Choose the official Windows Bedrock server ZIP, not Linux or a world backup.')
            if any(n.startswith(('worlds/', 'backups/', 'logs/')) for n in names):
                raise ValueError('This archive contains existing server data. Download a clean official server ZIP.')
            if shutil.disk_usage(root.parent).free < sum(m.file_size for m in members) + 512*1024**2:
                raise ValueError('Not enough free disk space. Allow space for the server and future worlds/backups.')
            extract_zip(package, stage, max_bytes=4*1024**3, max_files=100000)
        # Never import user identities or operator grants from an archive.
        atomic_json(stage / 'allowlist.json', [])
        atomic_json(stage / 'permissions.json', [])
        props = (stage / 'server.properties').read_text(encoding='utf-8-sig')
        defaults = {'server-name': 'My Bedrock Server', 'level-name': 'My World',
                    'online-mode': 'true', 'allow-list': 'true',
                    'default-player-permission-level': 'member', 'allow-cheats': 'false'}
        for key, value in defaults.items():
            pattern = rf'(?m)^{re.escape(key)}=.*$'
            props = re.sub(pattern, f'{key}={value}', props) if re.search(pattern, props) else props + f'\n{key}={value}\n'
        atomic_text(stage / 'server.properties', props)
        validate_server(stage)
        if root.exists():
            # Only this known-empty folder may be removed; never recurse here.
            root.rmdir()
        stage.rename(root)
    return root


class SetupWindow(tk.Tk):
    def __init__(self, previous=''):
        super().__init__()
        self.result = None
        self.busy = False
        self.events = queue.Queue()
        self.title(PRODUCT_NAME + ' · Server Setup')
        apply_window_icon(self)
        self.geometry('800x690')
        self.minsize(760, 660)
        self.configure(bg=BG)
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('TFrame', background=BG)
        style.configure('TLabel', background=BG, foreground=FG, font=('Segoe UI', 10))
        style.configure('TButton', background=CARD, foreground=FG, bordercolor=LINE,
                        lightcolor=LINE, darkcolor=LINE, padding=(12, 6), font=('Segoe UI', 10))
        style.map('TButton', background=[('active', HOVER)], foreground=[('disabled', FG_FAINT)])
        style.configure('Ignition.TButton', background=IGNITION, foreground=BG, bordercolor=IGNITION,
                        lightcolor=IGNITION, darkcolor=IGNITION, font=('Segoe UI', 10, 'bold'))
        style.map('Ignition.TButton', background=[('disabled', CARD), ('active', IGNITION_HOVER)],
                  foreground=[('disabled', FG_FAINT), ('active', BG)])
        style.configure('TEntry', fieldbackground=INPUT, foreground=FG, insertcolor=FG,
                        bordercolor=LINE, lightcolor=LINE, darkcolor=LINE, padding=(8, 6))
        style.map('TEntry', bordercolor=[('focus', PORTAL)])
        for control in ('TRadiobutton', 'TCheckbutton'):
            style.configure(control, background=BG, foreground=FG, font=('Segoe UI', 10),
                            indicatorbackground=INPUT, indicatorforeground=PORTAL)
            style.map(control, background=[('active', BG)], foreground=[('active', PORTAL)])
        style.configure('Horizontal.TProgressbar', background=PORTAL, troughcolor=INPUT,
                        bordercolor=INPUT, lightcolor=PORTAL, darkcolor=PORTAL)
        box = ttk.Frame(self, padding=24)
        box.pack(fill='both', expand=True)
        hero = ttk.Frame(box)
        hero.pack(fill='x', pady=(0, 4))
        art = tk.Canvas(hero, width=132, height=104, bg=BG, highlightthickness=0)
        art.pack(side='right')
        draw_shapes(art, portal_scene('online'), 0, 0, 132, 104)
        words = ttk.Frame(hero)
        words.pack(side='left', fill='both', expand=True)
        ttk.Label(words, text='BEDROCK SERVER MANAGER', foreground=PORTAL,
                  font=('Segoe UI', 9, 'bold')).pack(anchor='w', pady=(6, 0))
        ttk.Label(words, text='Welcome to FlintDock.', font=('Segoe UI', 22, 'bold')).pack(anchor='w', pady=(6, 4))
        ttk.Label(words, text='Your server starts with a spark.', foreground=IGNITION).pack(anchor='w')
        ttk.Label(box, text='Keep your worlds separate from the app. No Python installation needed.\n'
                  'Removing FlintDock keeps your server folders and settings.', wraplength=700).pack(anchor='w', pady=(0, 10))
        self.mode = tk.StringVar(value='existing' if previous else 'new')
        ttk.Radiobutton(box, text='Create a new server from an official Windows server ZIP', variable=self.mode, value='new', command=self.mode_changed).pack(anchor='w', pady=2)
        ttk.Radiobutton(box, text='Connect an existing server (its configuration is kept)', variable=self.mode, value='existing', command=self.mode_changed).pack(anchor='w', pady=2)
        ttk.Label(box, text='1  Choose a dedicated server data folder', font=('Segoe UI', 11, 'bold')).pack(anchor='w', pady=(14, 8))
        row = ttk.Frame(box)
        row.pack(fill='x')
        self.location = tk.StringVar(value=previous or str(Path.home() / 'Documents/FlintDock Servers/My Server'))
        ttk.Entry(row, textvariable=self.location).pack(side='left', fill='x', expand=True)
        ttk.Button(row, text='Browse…', command=self.browse_folder).pack(side='right', padx=(8, 0))
        self.new_box = ttk.Frame(box)
        self.new_box.pack(fill='x', pady=10)
        ttk.Label(self.new_box, text='2  Download the server from Minecraft', font=('Segoe UI', 11, 'bold')).pack(anchor='w')
        ttk.Label(self.new_box, text='Choose the Windows ZIP on the official site. Minecraft is not included.').pack(anchor='w', pady=5)
        links = ttk.Frame(self.new_box)
        links.pack(anchor='w', pady=5)
        ttk.Button(links, text='Open official download', command=lambda: webbrowser.open(DOWNLOAD)).pack(side='left')
        ttk.Button(links, text='Read Minecraft EULA', command=lambda: webbrowser.open(EULA)).pack(side='left', padx=8)
        row = ttk.Frame(self.new_box)
        row.pack(fill='x', pady=6)
        self.archive = tk.StringVar()
        ttk.Entry(row, textvariable=self.archive).pack(side='left', fill='x', expand=True)
        ttk.Button(row, text='Select ZIP…', command=self.browse_zip).pack(side='right', padx=(8, 0))
        self.accepted = tk.BooleanVar(value=False)
        ttk.Checkbutton(self.new_box, text='I downloaded this ZIP from Minecraft and accept its EULA.', variable=self.accepted).pack(anchor='w')
        self.status = tk.StringVar(value='New servers are allowlist-only. Add yourself in Players before connecting.')
        self.status_label = ttk.Label(box, textvariable=self.status, foreground=FG_DIM, wraplength=700)
        self.status_label.pack(anchor='w', pady=8)
        self.progress = ttk.Progressbar(box, mode='indeterminate')
        self.progress.pack(fill='x', pady=(0, 8))
        footer = ttk.Frame(box)
        footer.pack(side='bottom', fill='x', pady=(6, 0))
        self.go = ttk.Button(footer, text='Open FlintDock', style='Ignition.TButton', command=self.submit)
        self.go.pack(side='right')
        ttk.Button(footer, text='Cancel', command=self.cancel).pack(side='right', padx=10)
        ttk.Label(footer, text=f'FlintDock {VERSION} · Independent software', foreground=FG_DIM).pack(side='left')
        self.protocol('WM_DELETE_WINDOW', self.cancel)
        self.mode_changed()

    def mode_changed(self):
        if self.mode.get() == 'existing':
            self.new_box.pack_forget()
            self.status.set('Select the folder containing bedrock_server.exe and server.properties.\nBack up your existing server before making configuration changes.')
        else:
            self.new_box.pack(fill='x', pady=10, before=self.status_label)
            self.status.set('New servers are allowlist-only. Add yourself in Players before connecting.')

    def browse_folder(self):
        value = filedialog.askdirectory(parent=self, title='Choose server data folder', mustexist=False)
        if value:
            self.location.set(value)

    def browse_zip(self):
        value = filedialog.askopenfilename(parent=self, title='Official Windows Bedrock server ZIP', filetypes=[('ZIP archives', '*.zip')])
        if value:
            self.archive.set(value)

    def cancel(self):
        if not self.busy:
            self.destroy()

    def submit(self):
        if self.busy:
            return
        mode, dest, archive, accepted = self.mode.get(), self.location.get().strip(), self.archive.get().strip(), self.accepted.get()
        if not dest or (mode == 'new' and not archive):
            messagebox.showerror('Complete setup', 'Select a server folder and, for a new server, its official ZIP.', parent=self)
            return
        self.busy = True
        self.go.state(['disabled'])
        self.progress.start()
        self.status.set('Checking the server and preparing files… Please keep this window open.')
        def work():
            try:
                result = install_server_archive(archive, dest, accepted) if mode == 'new' else validate_server(Path(dest))
                atomic_json(preferences_path(), {'schema': 1, 'server_folder': str(result)})
                self.events.put((True, result))
            except Exception as exc:
                self.events.put((False, str(exc)))
        threading.Thread(target=work, daemon=True).start()
        self.after(100, self.poll)

    def poll(self):
        try:
            success, value = self.events.get_nowait()
        except queue.Empty:
            self.after(100, self.poll)
            return
        self.busy = False
        self.progress.stop()
        self.go.state(['!disabled'])
        if success:
            self.result = value
            self.destroy()
        else:
            self.status.set('Setup did not complete. Check the message and try again.')
            messagebox.showerror('Setup needs attention', value, parent=self)


def choose_server(force=False):
    previous = ''
    try:
        previous = json.loads(preferences_path().read_text(encoding='utf-8'))['server_folder']
        if not isinstance(previous, str):
            previous = ''
        if not force:
            return validate_server(Path(previous))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    setup = SetupWindow(previous)
    setup.mainloop()
    return setup.result
