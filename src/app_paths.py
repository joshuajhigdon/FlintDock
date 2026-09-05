"""Read-only application resources are separate from customer server data."""
from pathlib import Path
import sys

VERSION = '1.3.0'
PRODUCT_NAME = 'FlintDock'
PRODUCT_DESCRIPTION = 'Bedrock Server Manager'
CODE_ROOT = Path(__file__).resolve().parent
APP_ROOT = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else CODE_ROOT
WORKERS = frozenset({'server_manager', 'bedrock_addons', 'bedrock_update',
                     'build_admin_addon', 'build_mod_menu', 'launcher_health'})


def worker_command(module, *args):
    module = module.removesuffix('.py')
    if module not in WORKERS:
        raise ValueError('Unsupported launcher helper.')
    if getattr(sys, 'frozen', False):
        worker = APP_ROOT / 'FlintDockWorker.exe'
        if not worker.is_file():
            raise RuntimeError('The launcher worker is missing. Repair by reinstalling the launcher.')
        return [str(worker), '--worker', module, *map(str, args)]
    python = Path(sys.executable)
    if python.name.lower() == 'pythonw.exe':
        python = python.with_name('python.exe')
    return [str(python), '-u', str(CODE_ROOT / (module + '.py')), *map(str, args)]
