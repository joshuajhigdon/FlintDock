"""Shared, read-only command reference and bounded installed-pack discovery.

No pack code is imported or executed. command_help.json is an optional explicit
documentation contract for packs whose command registrations are dynamic.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re

from bedrock_storage import atomic_text, world_path
from build_mod_menu import resolve_name, describe_function

ROOT = Path(__file__).resolve().parent
DEFINITIONS = ROOT / 'command_reference.json'
PACK_UUID = '7b3c1e42-9f5a-4d18-8c6b-2a4e7d905f11'
DOCS = 'https://learn.microsoft.com/en-us/minecraft/creator/commands/commands/'
MAX_COMMANDS = 600


def clean(value, limit=500):
    return re.sub(r'[\x00-\x1f\x7f]', ' ', re.sub(r'§.', '', str(value or ''))).strip()[:limit]


def read_object(path: Path, limit=256*1024, *, comments=False):
    if path.is_symlink() or path.stat().st_size > limit:
        raise ValueError(f'Linked or oversized documentation: {path.name}')
    text = path.read_text(encoding='utf-8-sig')
    data = json.loads(without_comments(text) if comments else text)
    if not isinstance(data, dict):
        raise ValueError(f'{path.name} must contain an object.')
    return data


def definitions(path=DEFINITIONS):
    data = read_object(Path(path))
    if data.get('schema') != 1:
        raise ValueError('Unsupported command reference schema; update the bundled tooling.')
    for group in ('core', 'admin', 'manager'):
        entries = data.get(group)
        if not isinstance(entries, list) or not entries or len(entries) > 100:
            raise ValueError(f'Invalid {group} command definitions.')
        seen = set()
        for entry in entries:
            if (not isinstance(entry, dict) or not re.fullmatch(r'[a-z][a-z0-9_]*', str(entry.get('name', '')))
                    or not entry.get('summary') or entry['name'] in seen
                    or entry.get('param', '') not in ('', 'optional', 'required', 'message', 'query')):
                raise ValueError(f'Invalid or duplicate {group} command definition.')
            seen.add(entry['name'])
    return data


def base_entries(data):
    entries = []
    for group, category in [('core', 'Core server'), ('admin', 'Admin tools'), ('manager', 'Restart manager')]:
        for item in data[group]:
            name = item['name']
            suffix = {'optional': ' [player]', 'required': ' <player>', 'message': ' "message"', 'query': ' [search]'}.get(item.get('param'), '')
            command = '/' + name if group == 'core' else '/admin:' + name if group == 'admin' else '/scriptevent mgr:' + name
            entries.append({
                'id': group + ':' + name, 'category': category, 'title': command,
                'syntax': item.get('syntax', command + suffix), 'summary': item['summary'],
                'example': item.get('example', command + (' "Player Two"' if item.get('param') == 'required' else '')),
                'permission': item.get('permission', 'Operator'),
                'where': item.get('where', 'In game only' if group != 'core' else 'In game / server console'),
                'cheats': item.get('cheats', 'No' if group == 'admin' else 'Yes'),
                'notes': item.get('notes', ''), 'status': 'Bundled reference',
                'source': DOCS + name + '?view=minecraft-bedrock-stable' if group == 'core' else 'Bundled command_reference.json',
            })
    return entries


def without_comments(text):
    # Preserve quoted strings while masking comments. This is deliberately not a JS evaluator.
    pattern = re.compile(r'''("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)|//[^\n]*|/\*[\s\S]*?\*/''')
    return pattern.sub(lambda match: match.group(1) or ' ', text)


def bounded_files(folder: Path, suffix: str, limit: int, warnings):
    """Do not recurse into links or materialize an entire untrusted pack tree."""
    count = visited = 0
    for current, dirs, files in os.walk(folder, followlinks=False):
        safe_dirs = sorted(d for d in dirs if not (Path(current) / d).is_symlink()
                           and (Path(current) / d).resolve().is_relative_to(folder.resolve()))
        if len(safe_dirs) > 200:
            warnings.append(f'{folder.parent.name}: directory scan truncated at 200 children.')
        dirs[:] = safe_dirs[:200]
        visited += 1
        if visited > 400:
            warnings.append(f'{folder.parent.name}: directory scan limited to 400 folders.')
            return
        for name in sorted(files):
            if name.endswith(suffix):
                yield Path(current) / name
                count += 1
                if count >= limit:
                    return


def pack_commands(folder: Path):
    commands, warnings = [], []
    explicit = folder / 'command_help.json'
    if explicit.exists():
        try:
            data = read_object(explicit, 128*1024)
            if data.get('schema') != 1 or not isinstance(data.get('commands'), list):
                raise ValueError('Expected schema 1 and a commands list.')
            for item in data['commands'][:100]:
                if not isinstance(item, dict) or not item.get('syntax') or not item.get('summary'):
                    raise ValueError('Each documented command needs syntax and summary.')
                entry = {key: clean(item.get(key), 800 if key == 'notes' else 250) for key in
                    ('syntax', 'summary', 'example', 'permission', 'where', 'notes')}
                entry.update(title=entry['syntax'], evidence='Pack documentation: command_help.json',
                             source='command_help.json', cheats='Pack-specific; check documentation')
                commands.append(entry)
        except (OSError, ValueError) as exc:
            warnings.append(f'{folder.name}: {exc}')
    scripts = folder / 'scripts'
    budget = 8*1024**2
    if scripts.is_dir() and not scripts.is_symlink():
        for index, path in enumerate(bounded_files(scripts, '.js', 201, warnings)):
            if index >= 200:
                warnings.append(f'{folder.name}: script scan limited to 200 files.'); break
            if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):
                warnings.append(f'{folder.name}: linked script skipped.'); continue
            size = path.stat().st_size
            if size > 1024**2 or size > budget:
                warnings.append(f'{folder.name}: oversized script skipped; dynamic commands may be missing.'); continue
            budget -= size
            text = without_comments(path.read_text(encoding='utf-8', errors='replace'))
            for match in re.finditer(r'''registerCommand\s*\(\s*\{\s*name\s*:\s*["']([a-z0-9_]+:[a-z0-9_]+)["']''', text):
                if len(commands) >= 120:
                    warnings.append(f'{folder.name}: help limited to 120 entries.'); break
                command = '/' + match.group(1)
                commands.append({'title': command, 'syntax': command + ' (parameters: check /help)',
                    'summary': 'Literal custom-command registration detected in this pack.',
                    'example': '/help ' + match.group(1), 'permission': 'Pack-specific; verify with /help',
                    'where': 'Pack-specific', 'cheats': 'Pack-specific', 'source': path.relative_to(folder).as_posix(),
                    'evidence': 'Static discovery, not runtime verification',
                    'notes': 'Registration may be conditional. Dynamic names, chat-prefix commands and NPC/script events are not guessed.'})
    functions = folder / 'functions'
    if functions.is_dir() and not functions.is_symlink():
        for index, path in enumerate(bounded_files(functions, '.mcfunction', 61, warnings)):
            if index >= 60:
                warnings.append(f'{folder.name}: function help limited to 60 entries.'); break
            if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):
                continue
            if path.stat().st_size > 128*1024:
                warnings.append(f'{folder.name}: oversized function skipped.'); continue
            name = path.relative_to(functions).with_suffix('').as_posix()
            if not re.fullmatch(r'[a-zA-Z0-9_./-]+', name):
                continue
            commands.append({'title': '/function ' + name, 'syntax': '/function ' + name,
                'summary': describe_function(path) or 'Function file found in this pack; purpose is not documented.',
                'example': '/help function', 'permission': 'Operator; pack may have additional checks',
                'where': 'In game / server console', 'cheats': 'Yes', 'source': path.relative_to(folder).as_posix(),
                'evidence': 'Function file found; not verified as a public/admin command',
                'notes': 'May be internal, tick-driven or destructive. Help never runs this function. Consult the pack author first.'})
    unique = {}
    for entry in commands:
        key = entry['title'].split(' ')[0]
        if key in ('/function', '/scriptevent'):
            key = ' '.join(entry['title'].split(' ')[:2])
        unique.setdefault(key, entry)
    if len(unique) > 120:
        warnings.append(f'{folder.name}: help limited to 120 entries.')
    return list(unique.values())[:120], warnings


def discover(root: Path):
    root = Path(root).resolve()
    registration = world_path(root) / 'world_behavior_packs.json'
    active = set()
    if registration.exists():
        raw = json.loads(registration.read_text(encoding='utf-8-sig'))
        if not isinstance(raw, list):
            raise ValueError('World behavior-pack registration must be a list.')
        active = {str(item['pack_id']).lower() for item in raw if isinstance(item, dict) and 'pack_id' in item}
    state_path = root / 'mods' / '_addon_state.json'
    state = read_object(state_path, 2*1024**2) if state_path.exists() else {}
    if any(not isinstance(record, dict) for record in state.values()):
        raise ValueError('Addon state contains an invalid record; repair it before scanning help.')
    owned = {str(p.get('uuid', '')).lower() for record in state.values()
             for p in record.get('packs', []) if isinstance(p, dict)}
    packs, entries, warnings = [], [], []
    for folder in sorted((root / 'behavior_packs').glob('*')):
        if not folder.is_dir() or folder.is_symlink() or not (folder / 'manifest.json').is_file():
            continue
        try:
            manifest = read_object(folder / 'manifest.json', comments=True)
            header = manifest.get('header', {})
            uuid = str(header.get('uuid', '')).lower()
            if not uuid or uuid == PACK_UUID:
                continue
            if uuid not in active | owned and folder.name.startswith(('vanilla', 'chemistry', 'server_', 'editor', 'experimental_')):
                continue
            name = clean(resolve_name(folder, str(header.get('name', '')), folder.name), 100)
            status = 'Active in world' if uuid in active else 'Installed, disabled in world'
            found, issues = pack_commands(folder)
            warnings.extend(issues)
            version = header.get('version', [])
            packs.append({'name': name, 'uuid': uuid, 'version': '.'.join(map(str, version)), 'status': status, 'count': len(found)})
            for number, command in enumerate(found):
                command.update(id=f'mod:{uuid}:{number}', category='Installed mods', pack=name, status=status)
                entries.append(command)
            if len(entries) >= MAX_COMMANDS:
                warnings.append('Mod reference capped at 600 commands.'); break
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            warnings.append(f'{folder.name}: could not read command help ({exc}).')
    for archive, record in state.items():
        if record.get('disabled'):
            packs.append({'name': clean(archive, 120), 'uuid': '', 'version': '',
                'status': 'Uninstalled / archive disabled', 'count': 0})
    return entries[:MAX_COMMANDS], packs, sorted(set(warnings))


def build_reference(root: Path | None = None, definition_path=DEFINITIONS):
    data = definitions(definition_path)
    mod_entries, packs, warnings = discover(root) if root is not None else ([], [], [])
    reference = {'schema': 1, 'reviewed': data['reviewed'], 'entries': base_entries(data) + mod_entries,
        'packs': packs, 'warnings': warnings, 'notes':
        'Read-only guide. Core syntax is curated; /help <command> is authoritative for the running build. '
        'Mod discovery is best effort. Disabled/uninstalled packs are not available. Dynamic commands require pack documentation.'}
    reference['fingerprint'] = hashlib.sha256(json.dumps(reference, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return reference


def write_reference(target: Path, reference, definition_path=DEFINITIONS):
    data = definitions(definition_path)
    native = [[item['name'], item['summary'], item.get('param', '')] for item in data['admin']]
    atomic_text(target, '// GENERATED from command_reference.json and installed packs; do not edit.\n'
        + 'export const REFERENCE = ' + json.dumps(reference, ensure_ascii=False, indent=1) + ';\n'
        + 'export const ADMIN_COMMANDS = ' + json.dumps(native, ensure_ascii=False) + ';\n')


def entry_text(entry):
    return '\n\n'.join(filter(None, [entry.get('syntax', ''), entry.get('summary', ''),
        f"Where: {entry.get('where') or 'Pack-specific'}\nPermission: {entry.get('permission') or 'Pack-specific'}\nCheats: {entry.get('cheats', 'Unknown')}",
        'Example (reference only): ' + entry.get('example', ''), entry.get('notes', ''),
        'Status: ' + entry.get('status', ''), entry.get('evidence', ''), 'Source: ' + entry.get('source', '')]))


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', type=Path, help='Include installed-pack documentation from this server')
    parser.add_argument('--output', type=Path, required=True, help='Write a generated reference.js (use the installer for live packs)')
    args = parser.parse_args()
    write_reference(args.output, build_reference(args.server))
    print(f'Generated command reference: {args.output.resolve()}')
