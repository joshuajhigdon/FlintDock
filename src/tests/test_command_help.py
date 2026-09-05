"""Command discovery and installation checks use disposable server folders only."""
import json
from pathlib import Path
from unittest.mock import patch
import zipfile

from test_tooling import Isolated
from bedrock_storage import atomic_json, atomic_text, operation_lock
import command_help as helpdata
import build_admin_addon as builder
import bedrock_update as updater
import bedrock_recovery as recovery
from build_mod_menu import write_catalog


class CommandHelpTests(Isolated):
    def pack(self):
        folder = self.root / 'behavior_packs/Example'
        uuid = 'aaaa1111-bbbb-cccc-dddd-eeeeeeeeeeee'
        atomic_json(folder / 'manifest.json', {'format_version': 2, 'header': {
            'name': 'Example mod', 'uuid': uuid, 'version': [1, 0, 0]}})
        atomic_json(self.world / 'world_behavior_packs.json', [{'pack_id': uuid, 'version': [1, 0, 0]}])
        return folder, uuid

    def install(self, **kwargs):
        with patch.object(builder, 'server_running', return_value=False):
            return builder.install(self.root, **kwargs)

    def test_shared_reference_covers_native_commands_and_is_reproducible(self):
        reference = helpdata.build_reference()
        self.assertEqual(len(reference['entries']), 47)
        self.assertEqual(sum(e['category'] == 'Admin tools' for e in reference['entries']), 16)
        target = self.root / 'reference.js'
        helpdata.write_reference(target, reference)
        self.assertEqual(target.read_bytes(), (builder.SOURCE / 'scripts/reference.js').read_bytes())
        self.assertEqual(reference, helpdata.build_reference())
        self.assertIn('Server console only', helpdata.entry_text(next(e for e in reference['entries'] if e['id'] == 'core:stop')))

    def test_documented_commands_static_discovery_comments_and_internal_functions(self):
        folder, uuid = self.pack()
        atomic_json(folder / 'command_help.json', {'schema': 1, 'commands': [
            {'syntax': '/demo:heal [player]', 'summary': 'Documented heal'},
            {'syntax': '/scriptevent demo:menu', 'summary': 'Menu'},
            {'syntax': '/scriptevent demo:info', 'summary': 'Info'}]})
        atomic_text(folder / 'scripts/main.js', '// registerCommand({name: "fake:no"});\n'
            '/* registerCommand({name: "fake:no2"}); */\n'
            'registry.registerCommand({name: "demo:heal"});\nregistry.registerCommand({name: "demo:info"});')
        atomic_text(folder / 'functions/internal/tick.mcfunction', '# Internal housekeeping\nsay do-not-run')
        reference = helpdata.build_reference(self.root)
        entries = [e for e in reference['entries'] if e['category'] == 'Installed mods']
        self.assertEqual(len(entries), 5)
        self.assertEqual(reference['packs'][0]['status'], 'Active in world')
        self.assertFalse(any('fake:' in e['syntax'] for e in entries))
        self.assertTrue(any(e['summary'] == 'Documented heal' for e in entries))
        self.assertTrue(any('not runtime verification' in e.get('evidence', '') for e in entries))
        self.assertTrue(any('not verified as a public/admin command' in e.get('evidence', '') for e in entries))
        atomic_json(self.world / 'world_behavior_packs.json', [])
        self.assertEqual(helpdata.build_reference(self.root)['packs'][0]['status'], 'Installed, disabled in world')

    def test_disabled_archives_are_not_claimed_to_have_available_commands(self):
        atomic_json(self.root / 'mods/_addon_state.json', {'Other.mcaddon': {'disabled': True, 'packs': []}})
        reference = helpdata.build_reference(self.root)
        self.assertEqual(reference['packs'][0]['count'], 0)
        self.assertEqual(reference['packs'][0]['status'], 'Uninstalled / archive disabled')
        self.assertEqual(len(reference['entries']), 47)

    def test_bad_documentation_does_not_hide_valid_core_help(self):
        folder, _ = self.pack()
        atomic_json(folder / 'command_help.json', {'schema': 99, 'commands': []})
        atomic_text(folder / 'scripts/huge.js', 'x' * (1024**2 + 1))
        reference = helpdata.build_reference(self.root)
        self.assertEqual(len(reference['entries']), 47)
        self.assertEqual(len(reference['warnings']), 2)
        bad = self.root / 'definitions.json'
        atomic_json(bad, {'schema': 99})
        with self.assertRaises(ValueError):
            helpdata.build_reference(definition_path=bad)

    def test_stock_jsonc_manifests_do_not_break_discovery_or_first_install(self):
        atomic_text(self.root / 'behavior_packs/chemistry/manifest.json', '{\n// Stock pack comment\n'
                    '"header": {"uuid": "stock-chemistry", "version": [1,0,0], "name": "pack.name"}}')
        reference = helpdata.build_reference(self.root)
        self.assertEqual(reference['warnings'], [])
        self.assertEqual(reference['packs'], [])
        self.install(rebuild_catalog=True)

    def test_function_scan_is_capped_and_formatting_is_sanitized(self):
        folder, _ = self.pack()
        for number in range(70):
            atomic_text(folder / f'functions/f{number:02}.mcfunction', '# Test function')
        commands, warnings = helpdata.pack_commands(folder)
        self.assertEqual(len(commands), 60)
        self.assertTrue(warnings)
        self.assertEqual(helpdata.clean('§cRed\nLine\x00'), 'Red Line')

    def test_install_generates_same_help_in_archive_and_installed_pack(self):
        atomic_json(self.root / 'mods/_addon_state.json', {'Other.mcaddon': {'disabled': True, 'packs': []}})
        path = self.install()
        installed = self.root / builder.INSTALLED / 'scripts/reference.js'
        self.assertIn('Uninstalled / archive disabled', installed.read_text(encoding='utf-8'))
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(archive.read('RestartManagerLink/scripts/reference.js'), installed.read_bytes())

    def test_repeat_install_noop_and_corrupt_file_repair(self):
        self.install(rebuild_catalog=True)
        before = list((self.root / 'backups').glob('*.zip'))
        package = (self.root / 'mods' / builder.PACKAGE).read_bytes()
        self.install(rebuild_catalog=True)
        self.assertEqual(before, list((self.root / 'backups').glob('*.zip')))
        self.assertEqual(package, (self.root / 'mods' / builder.PACKAGE).read_bytes())
        atomic_text(self.root / builder.INSTALLED / 'scripts/help.js', '// corrupted')
        self.install()
        self.assertEqual((self.root / builder.INSTALLED / 'scripts/help.js').read_bytes(), (builder.SOURCE / 'scripts/help.js').read_bytes())
        self.assertEqual(recovery.operations(self.root, True), [])

    def test_explicit_enable_only_changes_own_disable_choice(self):
        self.install()
        atomic_json(self.root / 'mods/world_disabled.json', [builder.PACK_UUID, 'other-pack'])
        atomic_json(self.world / 'world_behavior_packs.json', [])
        with self.assertRaises(RuntimeError):
            self.install()
        with patch.object(builder, 'server_running', return_value=False):
            self.assertIn('disabled', builder.refresh_generated(self.root))
        self.install(enable=True)
        self.assertEqual(json.loads((self.root / 'mods/world_disabled.json').read_text()), ['other-pack'])

    def test_old_engine_and_newer_installed_bundle_are_not_overwritten(self):
        updater.write_marker(self.root, '1.21.0.3')
        with self.assertRaisesRegex(RuntimeError, 'require Bedrock'):
            self.install()
        self.assertFalse((self.root / builder.INSTALLED).exists())
        updater.write_marker(self.root, '1.26.45.1')
        self.install()
        path = self.root / builder.INSTALLED / 'manifest.json'
        manifest = json.loads(path.read_text())
        manifest['header']['version'] = [9, 0, 0]
        atomic_json(path, manifest)
        with self.assertRaisesRegex(RuntimeError, 'newer'):
            self.install()
        self.assertEqual(json.loads(path.read_text())['header']['version'], [9, 0, 0])

    def test_caller_held_lock_and_progress_work_without_nested_acquisition(self):
        stages = []
        with operation_lock(self.root), patch.object(builder, 'operation_lock', side_effect=AssertionError('Nested lock')):
            self.install(lock_held=True, progress=lambda d, t, s: stages.append(s))
        self.assertTrue(any('Backing up' in s for s in stages))
        self.assertIn('Installing', stages)

    def test_refresh_respects_disabled_profile_and_syncs_state_when_active(self):
        self.install()
        with patch.object(builder, 'server_running', return_value=False):
            self.assertIn('synchronized', builder.refresh_generated(self.root))
            atomic_json(self.world / 'world_behavior_packs.json', [])
            self.assertIn('disabled', builder.refresh_generated(self.root))
        self.assertEqual(json.loads((self.world / 'world_behavior_packs.json').read_text()), [])
