"""Offline diagnostic for the installed application; never selects a real server."""
import json
from pathlib import Path
import subprocess
import tempfile
import traceback


def run(launcher_class, args):
    from app_paths import VERSION, worker_command
    from bedrock_storage import atomic_json, atomic_text
    checks = []
    result = {'version': VERSION, 'checks': checks, 'ok': False}
    try:
        import sqlite3
        import ssl
        import tkinter
        from build_admin_addon import runtime_assets, SOURCE
        from first_run import SetupWindow
        checks.append(f'Bundled resources: {len(runtime_assets(SOURCE))} add-on files')
        db = sqlite3.connect(':memory:')
        db.execute('create table diagnostic(value text)')
        db.close()
        ssl.create_default_context()
        checks.append('SQLite and TLS runtime initialized')
        setup = SetupWindow()
        setup.withdraw()
        setup.update()
        setup.destroy()
        checks.append('First-run screen initialized')
        with tempfile.TemporaryDirectory(prefix='launcher-diagnostic-') as tmp:
            root = Path(tmp)
            atomic_text(root / 'server.properties', 'server-name=Diagnostic\nlevel-name=Diagnostic World\n')
            atomic_json(root / 'launcher_ui.json', {'update_check': 'off'})
            world = root / 'worlds/Diagnostic World/db'
            world.mkdir(parents=True)
            atomic_text(world / 'CURRENT', 'diagnostic-only')
            app = launcher_class(root, app_update_background=False)
            errors = []
            app.report_callback_exception = lambda *parts: errors.append(''.join(traceback.format_exception(*parts)))
            app.update()
            for page in app.NAV_ORDER:
                app.show_page(page)
                app.update()
            from player_permissions import permissions_snapshot, set_player_role
            from player_history import render_queue_command
            app.history.player_joined('Diagnostic Player', '123', '2026-01-01 10:00:00')
            app.history.player_left('Diagnostic Player', '2026-01-01 10:01:00')
            set_player_role(root, '123', 'visitor', permissions_snapshot(root)['revision'])
            app.history.queue_add('Diagnostic Player', 'give {player} bread')
            app.show_page('players')
            directory = app.player_directory
            directory.refresh()
            directory.tree.selection_set('Diagnostic Player')
            directory.pick()
            app.update()
            assert directory.people['Diagnostic Player']['role'] == 'visitor'
            assert directory.people['Diagnostic Player']['queued'] == 1
            assert not directory.people['Diagnostic Player']['online']
            assert render_queue_command('Diagnostic Player', 'give {player} bread') == 'give "Diagnostic Player" bread'
            checks.append('Offline player directory, persistent roles and command queue verified in disposable data')
            app.open_admin_quick_commands()
            app.update()
            app.app_updates.show()
            app.update()
            assert app.app_updates.settings['auto_download'] is False
            checks.append('Launcher update preferences/dialog initialized with downloads opt-in and network disabled for diagnostics')
            import io
            import hashlib
            import zipfile
            import flintdock_updates as app_updates
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w') as package:
                package.writestr('FlintDock/FlintDock.exe', b'MZ-diagnostic-not-executable')
                package.writestr('FlintDock/FlintDockWorker.exe', b'MZ-diagnostic-not-executable')
                package.writestr('FlintDock/_internal/diagnostic.txt', b'diagnostic-only')
            raw = buffer.getvalue()
            release = app_updates.Release('99.0.0', app_updates.RELEASES_URL,
                'FlintDock-99.0.0-Windows-x64-Standalone.zip',
                app_updates.RELEASES_URL + '/download/v99.0.0/FlintDock-99.0.0-Windows-x64-Standalone.zip',
                len(raw), hashlib.sha256(raw).hexdigest())
            class DiagnosticHTTP:
                def open(self, _url):
                    return io.BytesIO(raw)
            cached = app_updates.download_update(release, root / 'Diagnostic Update Cache', http=DiagnosticHTTP())
            assert cached.read_bytes() == raw
            assert not list(cached.parent.rglob('*.exe'))
            checks.append('Packaged app-update backend verified an in-memory synthetic download without network, extraction or execution')
            app.on_close()
            if errors:
                raise RuntimeError('; '.join(errors))
            checks.append('All launcher pages and admin quick-command dialog initialized')
            proc = subprocess.run(worker_command('server_manager', '--check', '--server', root),
                cwd=root, capture_output=True, encoding='utf-8', timeout=25,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if proc.returncode:
                raise RuntimeError(f'Worker exited {proc.returncode}: {proc.stderr}')
            checks.append('Packaged server-manager child process passed schedule check')
            proc = subprocess.run(worker_command('bedrock_addons', 'list', '--server', root),
                cwd=root, capture_output=True, encoding='utf-8', timeout=25,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if proc.returncode:
                raise RuntimeError(f'Add-on worker exited {proc.returncode}: {proc.stderr}')
            checks.append('Packaged add-on child process read an isolated server')
        result['ok'] = True
    except Exception:
        result['error'] = traceback.format_exc()
    if args:
        atomic_json(Path(args[0]), result)
    print(json.dumps(result, indent=2))
    return 0 if result['ok'] else 1
