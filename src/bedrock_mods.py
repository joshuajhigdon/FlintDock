"""Dependency checks, named pack profiles, and reversible addon installation."""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

from bedrock_storage import atomic_json, world_path
from bedrock_recovery import Transaction, create_restore_point

KINDS = {'behavior': ('behavior_packs', 'world_behavior_packs.json'),
         'resource': ('resource_packs', 'world_resource_packs.json')}
VANILLA_UUIDS = {'behavior': 'fe9f8597-5454-481a-8730-8d070a8e2e58',
                 'resource': '0575c61f-a5da-4b7f-9961-ffda2908861e'}


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return default


def pack_index(root: Path):
    result = {}
    duplicates = []
    for kind, (folder, _) in KINDS.items():
        for path in (Path(root) / folder).glob('*/manifest.json'):
            try:
                data = read_json(path, {})
                header = data['header']
                uuid = header['uuid'].lower()
                builtin = uuid == VANILLA_UUIDS[kind] and bool(re.fullmatch(r'vanilla(?:_[0-9.]+)?', path.parent.name))
                if builtin and path.parent.name != 'vanilla':
                    continue  # Bedrock bundles historical vanilla layers with shared UUIDs.
                item = {'uuid': uuid, 'kind': kind, 'name': header.get('name', path.parent.name),
                        'version': header['version'], 'folder': path.parent.name,
                        'dependencies': data.get('dependencies') or [], 'dir': path.parent, 'builtin': builtin}
                if uuid in result:
                    duplicates.append(uuid)
                result[uuid] = item
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue
    return result, sorted(set(duplicates))


def active_packs(root: Path):
    world = world_path(root)
    out = {}
    for kind, (_, file) in KINDS.items():
        data = read_json(world / file, [])
        if not isinstance(data, list):
            raise ValueError(f'{file} must contain an array.')
        out[kind] = data
    return out


def dependency_issues(index, enabled=None):
    enabled = set(index) if enabled is None else set(enabled)
    issues = []
    for uuid in sorted(enabled):
        pack = index.get(uuid)
        if not pack:
            issues.append(f'Missing pack: {uuid}')
            continue
        for dep in pack.get('dependencies', []):
            if not isinstance(dep, dict) or 'uuid' not in dep:
                continue  # Script modules are supplied by Bedrock, not pack folders.
            needed = str(dep['uuid']).lower()
            if needed not in index or (needed not in enabled and not index[needed].get('builtin')):
                issues.append(f"{pack['name']} needs pack {needed}, which is missing or disabled.")
            elif dep.get('version') != index[needed].get('version'):
                issues.append(f"{pack['name']} needs {index[needed]['name']} version {dep.get('version')}; found {index[needed].get('version')}.")
    return issues


def profiles(root: Path):
    data = read_json(Path(root) / 'mods' / 'profiles.json', {})
    if not isinstance(data, dict):
        raise ValueError('profiles.json must contain an object.')
    return data


def save_profile(root: Path, name: str):
    if not name.strip() or len(name.strip()) > 80:
        raise ValueError('Use a profile name between 1 and 80 characters.')
    data = profiles(root)
    data[name.strip()] = active_packs(root)
    atomic_json(Path(root) / 'mods' / 'profiles.json', data)


def compare_profile(root: Path, name: str):
    wanted = profiles(root)[name]
    current = active_packs(root)
    index, duplicates = pack_index(root)
    target_ids = {str(e['pack_id']).lower() for entries in wanted.values() for e in entries}
    current_ids = {str(e['pack_id']).lower() for entries in current.values() for e in entries}
    issues = dependency_issues(index, target_ids)
    for kind, entries in wanted.items():
        if kind not in KINDS or not isinstance(entries, list):
            raise ValueError('Invalid profile format.')
        for entry in entries:
            pack = index.get(str(entry['pack_id']).lower())
            if pack and (pack['kind'] != kind or pack['version'] != entry['version']):
                issues.append(f"Profile expects a different type or version of {pack['name']}.")
    return {'enable': sorted(target_ids-current_ids), 'disable': sorted(current_ids-target_ids),
            'issues': issues + [f'Duplicate UUID: {u}' for u in duplicates], 'target': wanted}


def apply_profile(root: Path, name: str, progress=None):
    changes = compare_profile(root, name)
    if changes['issues']:
        raise ValueError('\n'.join(changes['issues']))
    safety = create_restore_point(root, f'Before mod profile: {name}', progress)
    transaction = Transaction(root, 'Apply mod profile: ' + name, progress)
    try:
        world = world_path(root)
        for kind, (_, file) in KINDS.items():
            transaction.write_json(world / file, changes['target'].get(kind, []))
        disabled_path = Path(root) / 'mods' / 'world_disabled.json'
        disabled = set(read_json(disabled_path, []))
        disabled.update(changes['disable'])
        disabled.difference_update(changes['enable'])
        transaction.write_json(disabled_path, sorted(disabled))
        transaction.commit()
    except Exception:
        transaction.cancel()
        raise
    return safety


def install_packs(server, packs: list, record: dict | None, state: dict,
                  key: str, digest: str, dry=False, package_path: Path | None = None,
                  progress=None, clear_disabled=False):
    from bedrock_addons import safe_name
    from bedrock_recovery import timestamp, safe_target
    if package_path is not None:
        if Path(key).name != key or not Path(package_path).is_file():
            raise ValueError('A package update needs a plain archive filename and an existing package.')
        safe_target(server.root, 'mods/' + key)
    index, duplicates = pack_index(server.root)
    if duplicates:
        raise ValueError('Resolve duplicate installed pack UUIDs before installing: ' + ', '.join(duplicates))
    incoming = {p['uuid'].lower(): p for p in packs}
    if len(incoming) != len(packs):
        raise ValueError('The addon contains duplicate pack UUIDs.')
    for archive, info in state.items():
        if archive == key:
            continue
        for owned in info.get('packs', []):
            if owned['uuid'].lower() in incoming:
                raise ValueError(f"Pack {owned['uuid']} is already owned by {archive}.")
    old_ids = {p['uuid'].lower() for p in (record or {}).get('packs', [])}
    for uuid, pack in incoming.items():
        manifest = read_json(pack['dir'] / 'manifest.json', {})
        pack['dependencies'] = manifest.get('dependencies') or []
        if uuid in index and uuid not in old_ids:
            raise ValueError(f"Pack {uuid} already exists outside this addon; refusing to overwrite it.")
    index = {u: p for u, p in index.items() if u not in old_ids}
    index.update(incoming)
    registrations = active_packs(server.root)
    enabled = {str(e['pack_id']).lower() for entries in registrations.values() for e in entries}
    enabled.difference_update(old_ids)
    enabled.update(incoming)
    issues = dependency_issues(index, enabled)
    if issues:
        raise ValueError('\n'.join(issues))
    recorded = []
    targets = {}
    for old in (record or {}).get('packs', []):
        folder = server.pack_dir(old['kind']) / old['folder']
        if folder.resolve().parent != server.pack_dir(old['kind']).resolve():
            raise ValueError('Invalid installed pack folder.')
        targets[folder] = None
    for pack in packs:
        folder = f"{safe_name(pack['name'])}__{pack['uuid'][:8]}"
        target = server.pack_dir(pack['kind']) / folder
        safe_target(server.root, target.relative_to(server.root).as_posix())
        if target in targets and targets[target] is not None:
            raise ValueError(f'Two incoming packs resolve to the same folder: {folder}')
        if target.exists() and target not in targets:
            raise ValueError(f'Refusing to overwrite an unowned pack folder: {folder}')
        targets[target] = pack['dir']
        recorded.append({key: pack[key] for key in ('uuid', 'version', 'kind', 'name')})
        recorded[-1]['folder'] = folder
    if dry:
        return recorded
    create_restore_point(server.root, f'Before addon: {key}', progress)
    transaction = Transaction(server.root, 'Install addon: ' + key, progress)
    updated_state = dict(state)
    updated_state[key] = {'sha256': digest, 'installed_at': timestamp(), 'packs': recorded}
    try:
        for target, source in targets.items():
            transaction.replace(target, source)
        for kind, (_, file) in KINDS.items():
            entries = [e for e in registrations[kind] if str(e['pack_id']).lower() not in old_ids | set(incoming)]
            entries += [{'pack_id': p['uuid'], 'version': p['version']} for p in recorded if p['kind'] == kind]
            transaction.write_json(server.world / file, entries)
        transaction.write_json(server.state_path, updated_state)
        if clear_disabled:
            disabled_path = server.mods / 'world_disabled.json'
            disabled = read_json(disabled_path, [])
            if not isinstance(disabled, list):
                raise ValueError('World-disabled pack choices must be a list.')
            transaction.write_json(disabled_path, [u for u in disabled if str(u).lower() not in incoming])
        if package_path is not None:
            transaction.replace(server.mods / key, Path(package_path))
        transaction.commit()
        state.clear()
        state.update(updated_state)
    except Exception:
        transaction.cancel()
        raise
    return recorded
