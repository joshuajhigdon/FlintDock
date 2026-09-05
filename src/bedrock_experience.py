"""Settings history, backup catalogue, and local troubleshooting reports."""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import uuid
import zipfile

from bedrock_storage import atomic_json, atomic_text, verify_backup
from bedrock_recovery import operations, verify_restore_point


def properties(text):
    result = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition('=')
        if sep and not key.startswith('#'):
            result[key.strip()] = value.strip()
    return result


def settings_preview(text, proposed):
    current = properties(text)
    changes = [(k, current.get(k, ''), str(v)) for k, v in proposed.items() if current.get(k) != str(v)]
    lines = []
    seen = set()
    for line in text.splitlines():
        key, sep, _value = line.strip().partition('=')
        key = key.strip()
        if sep and not key.startswith('#') and key in proposed:
            line = f'{key}={proposed[key]}'
            seen.add(key)
        lines.append(line)
    lines.extend(f'{key}={value}' for key, value in proposed.items() if key not in seen)
    return '\n'.join(lines) + '\n', changes


def save_settings(root: Path, expected_text: str, new_text: str, label='Settings saved'):
    from launcher_health import validate_properties
    from bedrock_storage import world_path
    path = Path(root) / 'server.properties'
    current = path.read_text(encoding='utf-8-sig')
    if current != expected_text:
        raise RuntimeError('Settings changed on disk. Reload and review the new values before saving.')
    values = properties(new_text)
    errors = validate_properties(values)
    try:
        world_path(root, values.get('level-name', 'Bedrock level'))
    except ValueError as exc:
        errors.append(str(exc))
    if errors:
        raise ValueError('\n'.join(errors))
    if new_text == current:
        return None
    entry = {'created': datetime.now().isoformat(timespec='seconds'), 'label': label,
             'before': current, 'after': new_text,
             'changes': settings_preview(current, values)[1]}
    record = Path(root) / '.settings-history' / f'{datetime.now():%Y%m%d-%H%M%S-%f}-{uuid.uuid4().hex[:8]}.json'
    atomic_json(record, entry)
    atomic_text(path, new_text)
    return record


def settings_history(root: Path):
    out = []
    for path in sorted((Path(root) / '.settings-history').glob('*.json'), reverse=True)[:100]:
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            out.append({**data, 'file': path.name})
        except (OSError, ValueError):
            continue
    return out


def undo_settings(root: Path):
    current = (Path(root) / 'server.properties').read_text(encoding='utf-8-sig')
    last = next((entry for entry in settings_history(root) if entry['after'] == current), None)
    if not last:
        raise ValueError('There is no matching saved change to undo. External edits are preserved.')
    return save_settings(root, current, last['before'], 'Undo: ' + last['label'])


def backup_catalogue(root: Path):
    path = Path(root) / 'backups' / '.catalogue.json'
    try:
        stored = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        stored = {}
    result = []
    for archive_path in (Path(root) / 'backups').glob('*.zip'):
        st = archive_path.stat()
        fingerprint = [st.st_size, st.st_mtime_ns]
        saved = stored.get(archive_path.name, {})
        cached = saved if saved.get('fingerprint') == fingerprint else {}
        try:
            with zipfile.ZipFile(archive_path) as archive:
                metadata = json.loads(archive.comment or '{}')
                if 'restore-point.json' in archive.namelist():
                    metadata['kind'] = 'restore-point'
        except (OSError, ValueError, zipfile.BadZipFile):
            metadata = {'kind': 'unreadable'}
        inferred = re.sub(r'-(?:preupdate-|replaced-)?\d{8}-\d{6}.*$', '', archive_path.stem)
        result.append({'name': archive_path.name, 'path': archive_path,
                       'fingerprint': fingerprint, 'bytes': st.st_size, 'mtime': st.st_mtime,
                       'world': metadata.get('world', inferred), 'kind': metadata.get('kind', 'legacy'),
                       'label': cached.get('label', metadata.get('label', '')),
                       'verified': cached.get('verified', '')})
    return sorted(result, key=lambda item: (item['mtime'], item['name']), reverse=True)


def label_backup(root: Path, path: Path, label: str):
    update_backup_record(root, path, label=label[:100])


def update_backup_record(root, path, **updates):
    path = Path(path)
    if path.resolve().parent != (Path(root) / 'backups').resolve():
        raise ValueError('Select an archive in this server backup folder.')
    catalogue = Path(root) / 'backups' / '.catalogue.json'
    try:
        data = json.loads(catalogue.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        data = {}
    st = path.stat()
    before = data.get(path.name, {})
    fingerprint = [st.st_size, st.st_mtime_ns]
    if before.get('fingerprint') != fingerprint:
        before = {}
    data[path.name] = {**before, **updates, 'fingerprint': fingerprint}
    atomic_json(catalogue, data)


def verify_catalogued(root: Path, path: Path):
    with zipfile.ZipFile(path) as archive:
        complete = 'restore-point.json' in archive.namelist()
    result = verify_restore_point(path) if complete else verify_backup(path)
    update_backup_record(root, path, verified=datetime.now().isoformat(timespec='seconds'))
    return result


def tail(path: Path, max_bytes=256*1024):
    try:
        with path.open('rb') as stream:
            stream.seek(max(0, path.stat().st_size-max_bytes))
            return stream.read(max_bytes).decode('utf-8', errors='replace').splitlines()[-400:]
    except OSError:
        return []


def redact(text: str):
    text = re.sub(r'(?i)(xuid\s*[:=]\s*)\S+', r'\1[redacted]', text)
    # Four-part Bedrock versions resemble IPv4 addresses. Keep labelled versions.
    text = re.sub(r'\b(?:\d{1,3}\.){3}\d{1,3}\b',
        lambda hit: hit[0] if re.search(r'(?i)(?:version\s*[:=]?\s*|bedrock-server-|\bv)$',
                                       text[max(0, hit.start()-24):hit.start()]) else '[address]', text)
    text = re.sub(r'(?i)(token|password|secret)(\s*[:=]\s*)[^\s,}]+', r'\1\2[redacted]', text)
    return text


def incident_report(root: Path):
    from launcher_health import run_checks
    root = Path(root)
    lines = tail(root / 'server_manager.log')
    errors = [line for line in lines if re.search(r'\b(ERROR|WARN|failed|crash|exception)\b', line, re.I)
              and 'No targets matched selector' not in line and '[MGR]|' not in line]
    grouped = Counter(re.sub(r'^.*?\b(?:ERROR|WARN)\]\s*', '', line) for line in errors)
    hints = []
    joined = '\n'.join(errors).lower()
    for needle, hint in [('watchdog', 'Inspect recently changed scripts and packs for slow or repeated work.'),
                          ('memory', 'Compare RAM trends and reduce simulation distance before adding capacity.'),
                          ('dependency', 'Check the Mod profiles dependency report.'),
                          ('permission', 'Check file access and whether another server process is running.')]:
        if needle in joined:
            hints.append(hint)
    checks = run_checks(root)
    output = [f'Bedrock incident report — {datetime.now():%Y-%m-%d %H:%M:%S}',
              '', 'Health checks:']
    output += [f'[{r.level}] {r.title}: {r.detail}' for r in checks]
    output += ['', 'Repeated recent problems:']
    output += [f'{count}x {message}' for message, count in grouped.most_common(20)] or ['None found.']
    output += ['', 'Suggested checks (not a confirmed diagnosis):', *hints]
    output += ['', 'Recent settings changes:']
    for change in settings_history(root)[:5]:
        output.append(f"{change['created']} — {change['label']}")
        output.extend(f'  {key}: {before} -> {after}' for key, before, after in change['changes'])
    output += ['', 'Recent operations:']
    output += [f"{o.get('created', '')} {o.get('kind')} — {o.get('state')}" for o in operations(root)[:10]]
    output += ['', 'Recent console output:', *lines[-150:]]
    output += ['', 'Launcher exceptions:', *tail(root / 'launcher_errors.log')[-60:]]
    return redact('\n'.join(output).replace(str(root), '[server folder]'))
