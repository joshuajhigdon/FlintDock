"""Packaged entry point, isolated helper dispatch, and startup error handling."""
import json
import os
from pathlib import Path
import runpy
import sys
import traceback

from app_paths import VERSION, WORKERS, PRODUCT_NAME


def main(launcher_class):
    if sys.argv[1:2] == ['--worker']:
        if len(sys.argv) < 3 or sys.argv[2] not in WORKERS:
            print('Unknown helper. Reinstall if this was launched by the application.', file=sys.stderr)
            return 2
        module = sys.argv[2]
        sys.argv = [module, *sys.argv[3:]]
        runpy.run_module(module, run_name='__main__', alter_sys=True)
        return 0
    if sys.argv[1:2] == ['--version']:
        print(VERSION)
        return 0
    if sys.argv[1:2] == ['--self-test']:
        from release_selftest import run
        return run(launcher_class, sys.argv[2:])
    import tkinter as tk
    from tkinter import messagebox
    from first_run import choose_server, preferences_path, validate_server
    try:
        args = sys.argv[1:]
        if args and args[0] != '--setup':
            if len(args) != 1:
                raise ValueError('Use the launcher shortcut, or supply one server folder.')
            root = validate_server(Path(args[0]))
        else:
            root = choose_server(force=bool(args))
        if root is None:
            return 0
        app = launcher_class(root)
        app.title(PRODUCT_NAME + ' · ' + VERSION)
        app.mainloop()
        return 0
    except Exception as exc:
        log = preferences_path().parent / 'startup-error.log'
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(traceback.format_exc(), encoding='utf-8')
        except OSError:
            log = 'Unavailable (check folder permissions)'
        try:
            error = tk.Tk()
            error.withdraw()
            messagebox.showerror('Launcher could not open',
                f'{exc}\n\nUse the Server Setup shortcut to select another folder. '
                f'For a damaged installation, reinstall the launcher; your server files are kept.\n\nDetails: {log}')
            error.destroy()
        except Exception:
            pass
        return 1
