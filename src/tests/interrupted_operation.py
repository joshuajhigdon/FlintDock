"""Pause in a real transaction after its first live replacement for kill/recovery QA."""
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bedrock_recovery import Transaction
from bedrock_storage import atomic_text, operation_lock

root = Path(sys.argv[1]).resolve()
def progress(done, total, label):
    if done == 1:
        atomic_text(root / 'ready-to-kill', 'ready')
        time.sleep(30)
with operation_lock(root):
    transaction = Transaction(root, 'Crash recovery test', progress)
    transaction.replace(root / 'one.txt', root / 'new.txt')
    transaction.replace(root / 'two.txt', root / 'new.txt')
    transaction.commit()
