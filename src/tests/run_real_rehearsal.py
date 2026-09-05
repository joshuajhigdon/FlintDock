"""Opt-in actual BDS smoke test. Generates only a disposable, empty QA world.

Usage: python tests/run_real_rehearsal.py PATH_TO_OFFICIAL_ZIP VERSION
Never reads the installed world or starts the installed executable.
"""
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bedrock_rehearsal import rehearse
from bedrock_storage import atomic_json, atomic_text, operation_lock


def main():
    archive, version = Path(sys.argv[1]).resolve(), sys.argv[2]
    with tempfile.TemporaryDirectory(prefix='bedrock-real-qa-') as folder:
        root = Path(folder)
        (root / 'worlds' / 'Rehearsal QA').mkdir(parents=True)
        atomic_text(root / 'server.properties',
            'level-name=Rehearsal QA\nlevel-seed=442211\nmax-players=1\nview-distance=4\ntick-distance=4\n')
        def progress(done, total, label):
            print(label, flush=True)
        with operation_lock(root):
            report, _ = rehearse(root, archive, version, progress=progress)
        destination = ROOT / 'tests' / 'artifacts' / 'real-rehearsal.json'
        destination.parent.mkdir(exist_ok=True)
        atomic_json(destination, report)
        print(f"Passed: {report['passed']}; report: {destination}", flush=True)
        if report.get('failure'):
            print(report['failure'], flush=True)
        return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
