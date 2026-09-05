"""Build the maintained Restart Manager Link source; optionally install only this addon.

python build_admin_addon.py              # package into dist/, no live changes
python build_admin_addon.py --install    # stopped server, restore point + journal
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
from pathlib import Path
import shutil
import tempfile
import zipfile

from bedrock_addons import Server, discover_packs, sha256_file
from bedrock_mods import install_packs, read_json, active_packs
from bedrock_recovery import assert_recovered
from bedrock_storage import operation_lock
from bedrock_update import server_running, installed_version
import command_help

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / 'addon_src' / 'RestartManagerLink'
PACKAGE = 'RestartManagerLink.mcaddon'
PACK_UUID = '7b3c1e42-9f5a-4d18-8c6b-2a4e7d905f11'
ASSETS = ('manifest.json', 'scripts/main.js', 'scripts/admin.js', 'scripts/catalog.js',
          'scripts/help.js', 'scripts/reference.js')
INSTALLED = Path('behavior_packs/Restart_Manager_Link__7b3c1e42')


def runtime_assets(source: Path, catalog: Path | None = None):
    """Validate all inputs before creating an output or touching installed files."""
    source = Path(source)
    if source.is_symlink() or (source / 'scripts').is_symlink():
        raise ValueError('Linked source directories are not supported.')
    assets = {}
    for name in ASSETS:
        path = Path(catalog) if name == 'scripts/catalog.js' and catalog is not None else source / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'Missing or linked runtime asset: {name}')
        assets[name] = path
    manifest = json.loads(assets['manifest.json'].read_text(encoding='utf-8-sig'))
    if not isinstance(manifest, dict):
        raise ValueError('The addon manifest must be a JSON object.')
    header = manifest.get('header', {})
    if not isinstance(header, dict):
        raise ValueError('The addon header must be a JSON object.')
    version = header.get('version')
    modules = manifest.get('modules', [])
    if (header.get('uuid') != PACK_UUID or header.get('name') != 'Restart Manager Link'
            or not isinstance(version, list) or len(version) != 3
            or any(type(part) is not int or part < 0 for part in version)
            or not isinstance(modules, list) or len(modules) != 1 or not isinstance(modules[0], dict)
            or modules[0].get('entry') != 'scripts/main.js'
            or modules[0].get('uuid') != '7b3c1e42-9f5a-4d18-8c6b-2a4e7d905f22'
            or modules[0].get('type') != 'script' or modules[0].get('version') != version):
        raise ValueError('Invalid Restart Manager Link identity, entry point or version.')
    minimum = header.get('min_engine_version')
    if (not isinstance(minimum, list) or len(minimum) != 3
            or any(type(part) is not int or part < 0 for part in minimum)
            or manifest.get('dependencies') != [
                {'module_name': '@minecraft/server', 'version': '2.1.0'},
                {'module_name': '@minecraft/server-ui', 'version': '2.0.0'}]):
        raise ValueError('Unreviewed engine/API requirements. Update and test the bundled tooling together.')
    return assets


def compatibility(root: Path, source: Path = SOURCE):
    """Minimum-version check, not a promise that a future engine is compatible."""
    manifest = read_json(source / 'manifest.json', {})
    version = installed_version(root)
    minimum = tuple(manifest['header']['min_engine_version'])
    if version and tuple(int(p) for p in version.split('.')[:3]) < minimum:
        raise RuntimeError(f'In-game tools require Bedrock {".".join(map(str, minimum))} or newer; found {version}.')
    return (f'Bedrock {version}: minimum version met; rehearse future server updates.' if version else
            'Server build unknown: minimum version could not be checked. Rehearse before relying on these tools.')


def status(root: Path):
    """Cheap launcher summary. Full content and package checks happen on Install/update."""
    try:
        available = read_json(SOURCE / 'manifest.json', {})['header']['version']
        label = '.'.join(map(str, available))
        compatibility(root)
        state = read_json(root / 'mods/_addon_state.json', {})
        record = state.get(PACKAGE, {})
        disabled = read_json(root / 'mods/world_disabled.json', [])
        if record.get('disabled') or PACK_UUID in disabled:
            return f'In-game tools disabled · Install/update can enable v{label} with confirmation.'
        folder = root / INSTALLED
        if not folder.exists():
            return f'In-game tools not installed · Available v{label}'
        version = read_json(folder / 'manifest.json', {})['header']['version']
        if tuple(version) > tuple(available):
            return 'Newer in-game tools installed · Update launcher sources; automatic downgrade blocked.'
        if version != available:
            return f'In-game tools update available · v{".".join(map(str, version))} → v{label}'
        registrations = active_packs(root)['behavior']
        if (any(not (folder / name).is_file() or (folder / name).is_symlink() for name in ASSETS)
                or not any(str(e.get('pack_id')).lower() == PACK_UUID and e.get('version') == version for e in registrations)):
            return f'In-game tools v{label} need repair · Use Install/update.'
        return f'In-game tools v{label} registered · Install/update verifies files and refreshes help.'
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError) as exc:
        return f'In-game tools need attention: {exc}'


def build_package(source: Path, output: Path, catalog: Path | None = None):
    """Only runtime assets enter the distributable; exclude tests/caches/QA probes."""
    assets = runtime_assets(source, catalog)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.admin-build-', dir=output.parent) as folder:
        staged = Path(folder) / PACKAGE
        with zipfile.ZipFile(staged, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, path in assets.items():
                # Stable metadata: identical runtime bytes produce an identical package hash.
                entry = zipfile.ZipInfo('RestartManagerLink/' + name, date_time=(2020, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.external_attr = 0o100644 << 16
                archive.writestr(entry, path.read_bytes())
        staged.replace(output)
    return output


def install(root: Path, source: Path = SOURCE, *, lock_held=False, progress=None,
            enable=False, catalog_override: Path | None = None, rebuild_catalog=False):
    root, source = Path(root).resolve(), Path(source)
    with nullcontext() if lock_held else operation_lock(root):
        if server_running():
            raise RuntimeError('Stop the server before installing admin commands.')
        assert_recovered(root)
        runtime_assets(source)
        note = compatibility(root, source)
        if progress:
            progress(0, 0, note)
        server = Server(root)
        state = server.load_state()
        record = state.get(PACKAGE)
        disabled = read_json(server.mods / 'world_disabled.json', [])
        if not isinstance(disabled, list):
            raise ValueError('World-disabled pack choices must be a list.')
        if not enable and ((record and record.get('disabled')) or PACK_UUID in disabled):
            raise RuntimeError('Restart Manager Link is disabled. Enable it explicitly before updating it.')
        installed_manifest = read_json(root / INSTALLED / 'manifest.json', {})
        incoming_version = read_json(source / 'manifest.json', {})['header']['version']
        existing_version = installed_manifest.get('header', {}).get('version', [0, 0, 0])
        if tuple(existing_version) > tuple(incoming_version):
            raise RuntimeError('A newer Restart Manager Link is installed. Update the launcher sources; automatic downgrade is blocked.')
        with tempfile.TemporaryDirectory(prefix='.admin-install-', dir=root) as folder:
            stage = Path(folder)
            pack = stage / 'RestartManagerLink'
            # Catalogs are generated from each server's active mods; keep its latest one.
            catalog = catalog_override or root / INSTALLED / 'scripts/catalog.js'
            assets = runtime_assets(source, catalog if catalog.exists() else None)
            for name, path in assets.items():
                target = pack / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
            if rebuild_catalog:
                from build_mod_menu import scan_pack, write_catalog
                active = {str(e['pack_id']).lower() for e in active_packs(root)['behavior']}
                menu_packs = []
                for folder in sorted((root / 'behavior_packs').glob('*')):
                    if folder.is_symlink() or not folder.is_dir():
                        continue
                    try:
                        header = command_help.read_object(folder / 'manifest.json', comments=True).get('header', {})
                    except (OSError, ValueError):
                        continue  # Dependency validation below still rejects missing active packs.
                    if str(header.get('uuid', '')).lower() not in active - {PACK_UUID}:
                        continue
                    info = scan_pack(folder, False)
                    if info and (info['functions'] or info['toggles'] or info['config_items']):
                        menu_packs.append(info)
                write_catalog(pack / 'scripts/catalog.js', menu_packs)
            if progress:
                progress(0, 0, 'Building command help from installed packs (read-only scan)')
            command_help.write_reference(pack / 'scripts/reference.js', command_help.build_reference(root))
            package = build_package(pack, stage / PACKAGE)
            digest = sha256_file(package)
            packs = discover_packs(pack)
            # Idempotent updates still repair missing/corrupt files and registration drift.
            current_package = server.mods / PACKAGE
            registrations = active_packs(root)['behavior']
            own = [e for e in registrations if str(e.get('pack_id')).lower() == PACK_UUID]
            if (record and not record.get('disabled') and PACK_UUID not in disabled
                    and record.get('packs') == [{'uuid': PACK_UUID, 'version': incoming_version,
                        'kind': 'behavior', 'name': 'Restart Manager Link', 'folder': INSTALLED.name}]
                    and record.get('sha256') == digest and current_package.is_file()
                    and sha256_file(current_package) == digest
                    and own == [{'pack_id': PACK_UUID, 'version': incoming_version}]
                    and all((root / INSTALLED / name).is_file() and not (root / INSTALLED / name).is_symlink()
                            and (root / INSTALLED / name).read_bytes() == path.read_bytes()
                            for name, path in runtime_assets(pack).items())
                    and {p.relative_to(root / INSTALLED).as_posix() for p in (root / INSTALLED).rglob('*') if p.is_file()} == set(ASSETS)):
                if progress:
                    progress(1, 1, 'In-game tools already current; files and package verified')
                return current_package
            # The package, installed files, world registration and state change together.
            install_packs(server, packs, record, state, PACKAGE, digest, package_path=package,
                          progress=progress, clear_disabled=True)
    return root / 'mods' / PACKAGE


def refresh_generated(root: Path, *, lock_held=False, progress=None):
    """Refresh only an already-active bundle, respecting profile/disable choices."""
    with nullcontext() if lock_held else operation_lock(root):
        state = read_json(root / 'mods/_addon_state.json', {})
        record = state.get(PACKAGE, {})
        active = {str(e['pack_id']).lower() for e in active_packs(root)['behavior']}
        disabled = read_json(root / 'mods/world_disabled.json', [])
        if not (root / INSTALLED).exists() or record.get('disabled') or PACK_UUID in disabled or PACK_UUID not in active:
            return 'In-game help/menu not rebuilt: tools are absent or disabled. Use Install / update in-game tools to enable them.'
        install(root, lock_held=True, progress=progress, rebuild_catalog=True)
        return 'In-game mod menu and command help rebuilt; archive and installer state synchronized. Restart normally.'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true')
    parser.add_argument('--server', type=Path, default=ROOT)
    parser.add_argument('--enable', action='store_true', help='Explicitly enable only Restart Manager Link when installing')
    args = parser.parse_args()
    try:
        if not args.install:
            command_help.write_reference(SOURCE / 'scripts/reference.js', command_help.build_reference())
        result = install(args.server.resolve(), enable=args.enable) if args.install else build_package(SOURCE, ROOT / 'dist' / PACKAGE)
        print(f'{"Installed" if args.install else "Built"}: {result}')
        if args.install:
            print('Verified. Changed installs have a complete restore point and rollback journal. Start normally, then use /admin:help.')
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'Admin addon not installed: {exc}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
