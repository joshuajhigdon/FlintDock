"""Launch the actual manager runtime around a fake Bedrock subprocess."""
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server_manager import Manager
from bedrock_runtime import serve
from bedrock_storage import operation_lock

if __name__ == '__main__':
    root = Path(sys.argv[1]).resolve()
    args = SimpleNamespace(daemon=True, now=False,
                           server_command=[sys.executable, '-u', str(Path(__file__).with_name('fake_bds.py'))])
    with operation_lock(root):
        sys.exit(serve(Manager(root, args)))
