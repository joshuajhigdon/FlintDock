"""Pure preset validation: these tests never connect to a server."""
import json
import unittest
from types import SimpleNamespace

from admin_quick_commands import PRESETS, BY_ID, prepare, blocked_reason


class QuickCommandTests(unittest.TestCase):
    def test_all_28_presets_are_unique_single_commands_with_required_fields(self):
        self.assertEqual(len(PRESETS), 28)
        self.assertEqual(len(BY_ID), 28)
        values = {'player': 'Player Two', 'destination': 'Admin', 'message': 'Meet at spawn.', 'reason': 'Please rejoin.'}
        for preset in PRESETS:
            with self.subTest(preset=preset.id):
                command = prepare(preset.id, values, {'Player Two', 'Admin'})
                self.assertNotIn('\n', command)
                self.assertNotIn('\r', command)
                self.assertFalse(command.startswith(('admin:', 'save hold', 'op ', 'kill ', 'clear ')))
                self.assertTrue(preset.description)
                self.assertEqual(command.startswith('!'), preset.manager)

    def test_names_are_quoted_and_selectors_or_injection_are_rejected(self):
        self.assertEqual(prepare('heal', {'player': 'Player Two'}, {'Player Two'}),
                         'effect "Player Two" instant_health 1 10 true')
        for name in ['@a', '@e[type=player]', 'Bad"Name', 'Bad\\Name', 'Steve\n!quit', 'Steve\rstop',
                     '§cSteve', 'Steve\u2028stop', ' Steve', 'Steve ', '', 'x' * 65]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                prepare('heal', {'player': name}, {name})
        self.assertIn('"玩家 Two"', prepare('feed', {'player': '玩家 Two'}, {'玩家 Two'}))

    def test_player_presence_and_different_teleport_destination_are_required(self):
        for values, roster in [({'player': 'Left'}, {'Admin'}), ({'player': ''}, {'Admin'})]:
            with self.assertRaises(ValueError):
                prepare('heal', values, roster)
        with self.assertRaises(ValueError):
            prepare('teleport', {'player': 'Admin', 'destination': 'Admin'}, {'Admin'})
        with self.assertRaises(ValueError):
            prepare('teleport', {'player': 'Admin', 'destination': 'Left'}, {'Admin'})
        self.assertEqual(prepare('teleport', {'player': 'Player Two', 'destination': 'Admin'}, {'Player Two', 'Admin'}),
                         'tp "Player Two" "Admin" true')

    def test_messages_cannot_inject_console_or_manager_lines(self):
        for value in ['Hello\n!restart', 'Hello\rstop', '\tword', 'Hello\x00', 'Hello\u2029stop', '§4Warning', '', ' ' * 3, 'x' * 181]:
            for preset in ('message', 'announce', 'kick'):
                with self.subTest(value=value, preset=preset), self.assertRaises(ValueError):
                    prepare(preset, {'player': 'Admin', 'message': value, 'reason': value}, {'Admin'})
        command = prepare('announce', {'message': 'Quote "hello" and /kill @a are literal text.'}, set())
        payload = json.loads(command.removeprefix('tellraw @a '))
        self.assertEqual(payload['rawtext'][0]['text'], '[Admin] Quote "hello" and /kill @a are literal text.')

    def test_spawnpoint_uses_player_context_and_risky_actions_confirm(self):
        self.assertEqual(prepare('spawnpoint', {'player': 'Player Two'}, {'Player Two'}),
                         'execute as "Player Two" at @s run spawnpoint @s ~ ~ ~')
        for key in ('clear_effects', 'creative', 'survival', 'spectator', 'adventure', 'teleport',
                    'spawnpoint', 'day', 'night', 'clear_weather', 'freeze_time', 'resume_time', 'kick',
                    'restart', 'skip_restart', 'announce'):
            self.assertTrue(BY_ID[key].confirm, key)
        self.assertIn('not a cancellation', BY_ID['skip_restart'].description)
        self.assertEqual(prepare('skip_restart', {}, set()), '!skip')

    def test_unknown_ids_and_missing_fields_are_rejected(self):
        for key in ('!quit', 'arbitrary', None):
            with self.assertRaises(ValueError):
                prepare(key, {}, set())
        for preset in PRESETS:
            if preset.fields:
                with self.assertRaises(ValueError):
                    prepare(preset.id, {}, set())

    def test_state_gates_require_server_ready_no_maintenance_and_no_shutdown(self):
        app = SimpleNamespace(server_up=True, manager=SimpleNamespace(running=lambda: True))
        self.assertEqual(blocked_reason(app), '')
        for key, value in [('_maintenance', 'Backup'), ('_update_busy', True), ('_install_stage', True),
                           ('_stopping_on_purpose', True), ('server_up', False)]:
            old = getattr(app, key, None)
            setattr(app, key, value)
            self.assertTrue(blocked_reason(app), key)
            setattr(app, key, old)
        app.server_up = True
        app.manager.running = lambda: False
        self.assertTrue(blocked_reason(app))
