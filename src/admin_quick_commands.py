"""Typed, console-compatible admin presets. This module never sends commands.

Player-only /admin: commands are intentionally not impersonated from the console.
All user input is validated before formatting a single command line.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import unicodedata


@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    category: str
    command: str
    description: str
    fields: tuple[str, ...] = ()
    confirm: bool = False
    cheats: bool = True
    manager: bool = False


PRESETS = (
    Preset('list', 'List online players', 'Server checks', 'list', 'Request the server roster. Results appear in Console.', cheats=False),
    Preset('time', 'Read world time', 'Server checks', 'time query daytime', 'Read time without changing the day/night cycle.'),
    Preset('weather', 'Read Overworld weather', 'Server checks', 'execute in overworld run weather query', 'Read the current Overworld weather.'),
    Preset('allowlist', 'Show allowlist', 'Server checks', 'allowlist list', 'Read who is allowed to join; does not enable or change the allowlist.'),
    Preset('heal', 'Heal player', 'Player care', 'effect {player} instant_health 1 10 true',
           'Apply a strong instant-health effect to one player. Does not remove other effects.', ('player',)),
    Preset('feed', 'Feed player', 'Player care', 'effect {player} saturation 1 10 true',
           'Apply saturation briefly to refill hunger. Uses the vanilla effect, not the addon attribute API.', ('player',)),
    Preset('nightvision', 'Night vision · 5 minutes', 'Player care', 'effect {player} night_vision 300 0 true',
           'Help one player see in the dark for five minutes, without effect particles.', ('player',)),
    Preset('nightvision_off', 'Remove night vision', 'Player care', 'effect {player} clear night_vision',
           'Remove this effect, including night vision supplied by another source. Other effects remain.', ('player',)),
    Preset('clear_effects', 'Clear all player effects', 'Player care', 'effect {player} clear',
           'Remove ALL effects from one player, including beneficial potion and beacon effects.', ('player',), confirm=True),
    Preset('bread', 'Give 16 bread', 'Supplies', 'give {player} minecraft:bread 16',
           'Give a small food supply to one player. Repeated use gives additional items.', ('player',)),
    Preset('torches', 'Give 32 torches', 'Supplies', 'give {player} minecraft:torch 32',
           'Give one player lighting supplies. Repeated use gives additional items.', ('player',)),
    Preset('survival', 'Set Survival mode', 'Game modes', 'gamemode survival {player}',
           'Return one player to Survival. Falling, drowning or other hazards can immediately matter again.', ('player',), confirm=True),
    Preset('creative', 'Set Creative mode', 'Game modes', 'gamemode creative {player}',
           'Give one player Creative building abilities and access to items.', ('player',), confirm=True),
    Preset('adventure', 'Set Adventure mode', 'Game modes', 'gamemode adventure {player}',
           'Restrict normal block interaction for one player. This is not a permission or anti-grief system.', ('player',), confirm=True),
    Preset('spectator', 'Set Spectator mode', 'Game modes', 'gamemode spectator {player}',
           'Let one player observe without normal interaction. Use Survival when ready to return.', ('player',), confirm=True),
    Preset('teleport', 'Teleport player to player', 'Travel', 'tp {player} {destination} true',
           'Move the selected player to the destination player, checking block collisions. No saved /admin:back point; check terrain hazards first.',
           ('player', 'destination'), confirm=True),
    Preset('spawnpoint', 'Set spawn at player location', 'Travel', 'execute as {player} at @s run spawnpoint @s ~ ~ ~',
           'Set this player\'s respawn point at their current location/dimension, not at the console origin. Check the location before confirming.',
           ('player',), confirm=True),
    Preset('day', 'Set morning', 'World controls', 'time set day', 'Change the world time to morning for everyone.', confirm=True),
    Preset('night', 'Set night', 'World controls', 'time set night', 'Change the world time to night for everyone.', confirm=True),
    Preset('clear_weather', 'Clear skies · 10 minutes', 'World controls', 'execute in overworld run weather clear 600',
           'Request clear Overworld weather for ten minutes.', confirm=True),
    Preset('freeze_time', 'Freeze daylight cycle', 'World controls', 'gamerule dodaylightcycle false',
           'Persistently pause the natural day/night cycle. Use Resume daylight cycle to undo this rule change.', confirm=True, cheats=False),
    Preset('resume_time', 'Resume daylight cycle', 'World controls', 'gamerule dodaylightcycle true',
           'Persistently resume normal day/night progression. Does not restore an earlier clock time.', confirm=True, cheats=False),
    Preset('message', 'Private message / warning', 'Communication', 'tell {player} {message}',
           'Send a private message to one player. Like console commands, the text is visible in local server/launcher logs.', ('player', 'message'), cheats=False),
    Preset('announce', 'Announce to everyone', 'Communication', 'tellraw @a {announcement}',
           'Send a public message prefixed [Admin]. Preview it before broadcasting.', ('message',), confirm=True, cheats=False),
    Preset('kick', 'Kick player with reason', 'Moderation', 'kick {player} {reason}',
           'Disconnect one player with the supplied reason. This does not ban them.', ('player', 'reason'), confirm=True, cheats=False),
    Preset('next_restart', 'Show next restart', 'Restart manager', '!next',
           'Ask the Python manager to announce the next scheduled restart.', cheats=False, manager=True),
    Preset('restart', 'Restart · 1-minute warning', 'Restart manager', '!restart',
           'Request a managed restart with a one-minute warning. Watch Console for the countdown.', confirm=True, cheats=False, manager=True),
    Preset('skip_restart', 'Skip next scheduled restart', 'Restart manager', '!skip',
           'Skip the next scheduled restart. This is not a cancellation of a manual restart already requested.', confirm=True, cheats=False, manager=True),
)
BY_ID = {preset.id: preset for preset in PRESETS}
DEFAULT_FAVORITES = ('heal', 'feed', 'nightvision', 'teleport', 'day', 'clear_weather', 'message', 'restart')


def single_line(value: str, label: str, maximum: int):
    if not isinstance(value, str) or any(unicodedata.category(c).startswith('C') or c in '\u2028\u2029§' for c in value):
        raise ValueError(f'{label} must be plain, single-line text without formatting/control characters.')
    if not value.strip() or len(value) > maximum:
        raise ValueError(f'{label} must contain 1–{maximum} characters.')
    return value.strip()


def player_name(value: str, online=None):
    name = single_line(value, 'Player name', 64)
    if name != value or any(c in name for c in '@"\\[]'):
        raise ValueError('Select one named player. Selectors, quotes and escape characters are not accepted.')
    if online is not None and name not in online:
        raise ValueError(f'{name} is no longer in the online roster. Refresh players and select again.')
    return '"' + name + '"'


def prepare(preset_id: str, values: dict[str, str], online=None) -> str:
    """Validate and render. Pass the current roster for execution; None is preview only."""
    if preset_id not in BY_ID:
        raise ValueError('Choose a built-in quick command.')
    preset = BY_ID[preset_id]
    args = {}
    for key in preset.fields:
        if key in ('player', 'destination'):
            args[key] = player_name(values.get(key, ''), online)
        else:
            args[key] = single_line(values.get(key, ''), key.title(), 180)
    if 'destination' in args and args['player'].casefold() == args['destination'].casefold():
        raise ValueError('Choose a different destination player.')
    if preset_id == 'announce':
        args['announcement'] = json.dumps({'rawtext': [{'text': '[Admin] ' + args['message']}]}, ensure_ascii=False)
    command = preset.command.format(**args)
    if '\n' in command or '\r' in command:
        raise ValueError('A quick command must be one line.')
    return command


def blocked_reason(app):
    if getattr(app, '_maintenance', '') or getattr(app, '_update_busy', False) or getattr(app, '_install_stage', ''):
        return 'Wait for the current maintenance or update operation to finish.'
    if getattr(app, '_stopping_on_purpose', False):
        return 'The server is stopping. Commands are not queued for its next start.'
    if not app.server_up or not app.manager.running():
        return 'Start the server through the launcher and wait until it is ready.'
    return ''
