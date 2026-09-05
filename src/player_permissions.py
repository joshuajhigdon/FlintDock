"""XUID-based player roles. Never alter worlds, defaults, or unrelated grants."""
import hashlib
import json
import re
import threading
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from bedrock_storage import atomic_json, atomic_text

ROLES = ('visitor', 'member', 'operator')
_lock = threading.RLock()


def valid_xuid(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[0-9]{1,20}', value)) and int(value) > 0


def _path(root, name):
    root = Path(root).resolve()
    path = root / name
    if path.is_symlink() or path.resolve().parent != root:
        raise ValueError(f'{name} must stay inside the selected server folder.')
    return path


def permissions_snapshot(root):
    path = _path(root, 'permissions.json')
    raw = path.read_bytes() if path.exists() else None
    try:
        rows = json.loads(raw.decode('utf-8-sig')) if raw is not None else []
    except (UnicodeError, ValueError) as exc:
        raise ValueError('permissions.json is invalid. Repair it before changing roles; it was not overwritten.') from exc
    if not isinstance(rows, list):
        raise ValueError('permissions.json must be a list. No roles were changed.')
    seen = set()
    for row in rows:
        if (not isinstance(row, dict) or not valid_xuid(row.get('xuid'))
                or row.get('permission') not in ROLES or row['xuid'] in seen):
            raise ValueError('permissions.json has an invalid or duplicate entry. No roles were changed.')
        seen.add(row['xuid'])
    return {'rows': rows, 'revision': hashlib.sha256(raw).hexdigest() if raw is not None else 'missing',
            'raw': raw}


def directory_snapshot(root, history, online):
    """Read-only union: persisted history, allowlist, and the live connection set."""
    people = {p['name']: dict(p) for p in history.players()} if history else {}
    warnings = []
    try:
        path = _path(root, 'allowlist.json')
        entries = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else []
        if not isinstance(entries, list):
            raise ValueError('Expected an allowlist array.')
        for item in entries:
            if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not item['name'].strip():
                continue
            name = item['name']
            person = people.setdefault(name, {'name': name})
            person['allowlisted'] = True
            if not person.get('xuid') and valid_xuid(item.get('xuid')):
                person['xuid'] = item['xuid']
    except (OSError, ValueError) as exc:
        warnings.append(f'Allowlist could not be read: {exc}')
    for name in online:
        people.setdefault(name, {'name': name})
    default = 'member'
    try:
        for line in (Path(root) / 'server.properties').read_text(encoding='utf-8-sig').splitlines():
            key, sep, value = line.partition('=')
            if sep and key.strip() == 'default-player-permission-level':
                default = value.strip() if value.strip() in ROLES else 'member'
    except OSError as exc:
        warnings.append(f'Default role could not be read: {exc}')
        default = 'unknown'
    try:
        permissions = permissions_snapshot(root)
        roles = {r['xuid']: r['permission'] for r in permissions['rows']}
    except (OSError, ValueError) as exc:
        warnings.append(str(exc))
        permissions, roles = {'revision': None}, {}
    counts = history.queue_counts() if history else {}
    for name, person in people.items():
        person['online'] = name in online
        if not person['online'] and 'seconds' in person:
            from player_history import fmt_span
            person['playtime'] = fmt_span(person['seconds'] or 0)
        person['queued'] = counts.get(name, 0)
        person['role'] = roles.get(person.get('xuid'), default) if permissions['revision'] else 'unknown'
        person['role_source'] = 'Saved override' if person.get('xuid') in roles else 'Server default'
        person['last_seen'] = person.get('last_seen') or 'Not recorded'
        person['playtime'] = person.get('playtime') or '—'
    ordered = sorted(people.values(), key=lambda p: p['name'].casefold())
    ordered.sort(key=lambda p: p['last_seen'] if p['last_seen'] != 'Not recorded' else '', reverse=True)
    ordered.sort(key=lambda p: not p['online'])
    return {'players': ordered, 'revision': permissions['revision'], 'warnings': warnings}


def set_player_role(root, xuid, role, expected_revision):
    if not valid_xuid(xuid):
        raise ValueError('No verified Xbox ID is recorded. Have this player join once before changing their role.')
    if role not in ROLES:
        raise ValueError('Choose Visitor, Member or Operator.')
    with _lock:
        snapshot = permissions_snapshot(root)
        if expected_revision != snapshot['revision']:
            raise ValueError('Permissions changed since you opened this player. Refresh and review the role again.')
        rows = snapshot['rows']
        found = next((r for r in rows if r['xuid'] == xuid), None)
        if found and found['permission'] == role:
            return {'changed': False, 'role': role, 'backup': None}
        if found:
            found['permission'] = role
        else:
            rows.append({'xuid': xuid, 'permission': role})
        backup_dir = _path(root, 'permission-history')
        backup_dir.mkdir(exist_ok=True)
        backup = backup_dir / (datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid4().hex[:8] + '.json')
        # Preserve the exact previous text, including unknown fields and formatting.
        atomic_text(backup, snapshot['raw'].decode('utf-8-sig') if snapshot['raw'] is not None else '[]\n')
        if permissions_snapshot(root)['revision'] != expected_revision:
            raise ValueError('Permissions changed during the save. Nothing was overwritten; refresh and try again.')
        atomic_json(_path(root, 'permissions.json'), rows)
        return {'changed': True, 'role': role, 'backup': backup.name}
