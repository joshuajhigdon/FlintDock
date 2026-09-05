"""Brand, portal-state, rendering bounds and backward-compatible setup checks."""
import json
from pathlib import Path
import sys
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app_paths
import first_run
from launcher_theme import WorldArtwork, BG, PORTAL, IGNITION
from portal_art import portal_scene, portal_mark


class FlintDockTests(unittest.TestCase):
    def test_brand_and_version(self):
        self.assertEqual(app_paths.PRODUCT_NAME, 'FlintDock')
        self.assertEqual(app_paths.VERSION, '1.3.0')

    def test_free_use_terms_match_this_distribution(self):
        customer = Path(__file__).resolve().parents[2] / 'customer'
        license_text = (customer / 'LICENSE.txt').read_text(encoding='utf-8')
        self.assertIn('free of charge', license_text)
        self.assertIn('No purchase', license_text)
        self.assertNotIn('purchaser', license_text.lower())
        self.assertIn('FREE TO USE', (customer / 'START-HERE.txt').read_text(encoding='utf-8'))

    def test_scene_is_deterministic_and_bounded(self):
        for state in ('offline', 'starting', 'online', 'stopping'):
            shapes = portal_scene(state)
            self.assertEqual(shapes, portal_scene(state))
            self.assertLess(len(shapes), 300)
            for _, points, _ in shapes:
                self.assertTrue(all(0 <= x <= 240 for x in points[::2]))
                self.assertTrue(all(0 <= y <= 200 for y in points[1::2]))
        self.assertNotEqual(portal_scene('offline'), portal_scene('online'))

    def test_icon_is_bounded_and_has_ignition_color(self):
        self.assertTrue(any(color == IGNITION for _, _, color in portal_mark()))
        for _, points, _ in portal_mark():
            self.assertTrue(all(0 <= p <= 64 for p in points))

    def test_portal_tracks_state_without_idle_timers_or_item_leaks(self):
        root = tk.Tk()
        try:
            art = WorldArtwork(root, width=162, height=135)
            art.pack()
            root.update()
            before = root.tk.call('after', 'info')
            for text, expected in (('Running', 'online'), ('Starting', 'starting'),
                                    ('Stopping', 'stopping'), ('Stopped', 'offline'),
                                    ('External server', 'offline')) * 3:
                art.set_state(text)
                root.update()
                self.assertEqual(art.state, expected)
                self.assertLess(len(art.find_all()), 300)
            self.assertEqual(root.tk.call('after', 'info'), before)
        finally:
            root.destroy()

    def test_old_customer_setup_selection_still_loads(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict('os.environ', {'LOCALAPPDATA': folder}):
                config = first_run.preferences_path()
                self.assertEqual(config.parent.name, 'BedrockServerLauncher')
                config.parent.mkdir()
                config.write_text(json.dumps({'schema': 1, 'server_folder': 'CustomerServer'}))
                before = config.read_bytes()
                with patch.object(first_run, 'validate_server', return_value=Path('CustomerServer')) as validate:
                    self.assertEqual(first_run.choose_server(), Path('CustomerServer'))
                    validate.assert_called_once_with(Path('CustomerServer'))
                self.assertEqual(config.read_bytes(), before)

    def test_new_frozen_worker_filename(self):
        with tempfile.TemporaryDirectory() as folder:
            worker = Path(folder) / 'FlintDockWorker.exe'
            worker.touch()
            with patch.object(app_paths, 'APP_ROOT', Path(folder)), patch.object(sys, 'frozen', True, create=True):
                self.assertEqual(app_paths.worker_command('server_manager', '--check')[:3],
                                 [str(worker), '--worker', 'server_manager'])
