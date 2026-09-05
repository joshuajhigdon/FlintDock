"""A small real child process that implements the console contract used in tests."""
from pathlib import Path
import sys

print('[2026-09-04 10:00:00 INFO] Version: 1.0.0', flush=True)
print('[2026-09-04 10:00:00 INFO] Server started.', flush=True)
for line in sys.stdin:
    command = line.strip()
    if command == 'stop':
        print('[2026-09-04 10:01:00 INFO] Stopping server...', flush=True)
        print('Quit correctly', flush=True)
        break
    if command == 'test-join':
        print('[2026-09-04 10:00:10 INFO] Player connected: Alex, xuid: 123', flush=True)
    elif command == 'test-leave':
        print('[2026-09-04 10:00:20 INFO] Player disconnected: Alex, xuid: 123', flush=True)
    else:
        print('Accepted: ' + command, flush=True)
