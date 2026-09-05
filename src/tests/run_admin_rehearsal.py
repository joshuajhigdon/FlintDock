"""Opt-in real BDS addon smoke test; only generates a disposable empty world.

python tests/run_admin_rehearsal.py PATH_TO_OFFICIAL_ZIP VERSION
No game client is simulated; player interactions are covered by the JS unit suite.
"""
from pathlib import Path
import json
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bedrock_rehearsal import rehearse
from bedrock_storage import atomic_json, atomic_text, operation_lock


def main():
    archive, version = Path(sys.argv[1]).resolve(), sys.argv[2]
    source = ROOT / 'addon_src' / 'RestartManagerLink'
    with tempfile.TemporaryDirectory(prefix='bedrock-admin-qa-') as folder:
        root = Path(folder)
        world = root / 'worlds' / 'Admin QA'
        world.mkdir(parents=True)
        target = root / 'behavior_packs' / 'RestartManagerLink'
        shutil.copytree(source, target)
        shutil.copy2(ROOT / 'tests' / 'admin_probe.js', target / 'scripts' / 'qa.js')
        main_js = target / 'scripts' / 'main.js'
        atomic_text(main_js, main_js.read_text(encoding='utf-8') + '\nexport { refreshInfo };\nimport "./qa.js";\n')
        header = json.loads((target / 'manifest.json').read_text(encoding='utf-8'))['header']
        atomic_json(world / 'world_behavior_packs.json', [{'pack_id': header['uuid'], 'version': header['version']}])
        atomic_text(root / 'server.properties', 'level-name=Admin QA\nlevel-seed=442211\n'
                    'max-players=1\nview-distance=4\ntick-distance=4\nallow-cheats=false\n')
        with operation_lock(root):
            report, _ = rehearse(root, archive, version, progress=lambda d, t, s: print(s, flush=True), settle=10)
        report['addon_verified'] = (any('[ADMIN-QA] PASS:' in line for line in report['output'])
            and any('[ADMIN] Registered 16 operator commands.' in line for line in report['output']))
        report['passed'] = report['passed'] and report['addon_verified']
        report['addon_version'] = header['version']
        destination = ROOT / 'tests' / 'artifacts' / 'admin-rehearsal.json'
        destination.parent.mkdir(exist_ok=True)
        atomic_json(destination, report)
        print(f"Passed: {report['passed']}; report: {destination}", flush=True)
        for line in report['output']:
            if any(marker in line for marker in ('[ADMIN', '[Scripting]', 'ERROR', 'FATAL')):
                print(line, flush=True)
        return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
