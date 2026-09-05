"""Measure real Tk widget geometry at supported setup-window sizes."""
import json
from pathlib import Path
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
from first_run import SetupWindow

app = SetupWindow()
results = []
for size in ('800x690', '760x660', '1000x800'):
    app.geometry(size)
    app.update()
    leaves = []
    def walk(widget):
        if widget.winfo_children():
            for child in widget.winfo_children():
                walk(child)
        elif widget.winfo_class() not in ('TFrame', 'Frame'):
            leaves.append(widget)
    walk(app)
    failures = []
    for widget in leaves:
        text = str(widget.cget('text')) if 'text' in widget.keys() else widget.winfo_class()
        if not widget.winfo_ismapped():
            failures.append('Not visible: ' + text)
        elif widget.winfo_height() < widget.winfo_reqheight():
            failures.append(f'Too short: {text} ({widget.winfo_height()}<{widget.winfo_reqheight()})')
    results.append({'size':size, 'failures':failures})
app.destroy()
sys.path.insert(0, str(Path(__file__).resolve().parent / 'src/tests'))
from test_tooling import fixture, load_launcher
launcher = load_launcher()
with tempfile.TemporaryDirectory(prefix='flintdock-layout-') as folder:
    root = Path(folder)
    fixture(root)
    app = launcher.Launcher(root, app_update_background=False)
    try:
        for size in ('980x660', '980x740', '1280x900'):
            app.geometry(size)
            app.show_page('dashboard')
            for state in ('Stopped', 'Starting', 'Running', 'Stopping'):
                app._paint_pill(state, launcher.GREEN)
                app.update()
                label = app.world_state
                failures = []
                if not label.winfo_ismapped() or label.winfo_height() < label.winfo_reqheight():
                    failures.append('Portal status label clipped')
                if app.health_canvas.winfo_height() <= 45:
                    failures.append('Health panel too short')
                results.append({'window':'dashboard', 'size':size, 'state':state,
                                'health_height':app.health_canvas.winfo_height(),
                                'failures':failures})
    finally:
        app.on_close()
(Path(__file__).resolve().parent / 'qa-layout.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
print(json.dumps(results, indent=2))
raise SystemExit(1 if any(item['failures'] for item in results) else 0)
