import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import launch
from preferences import load_preferences, parse_port, save_preferences


class PreferenceTests(unittest.TestCase):
    def test_port_validation(self):
        self.assertEqual(parse_port(' 9876 '), 9876)
        for value in ('', 'abc', '0', '65536', '1.5', True, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_port(value)

    def test_defaults_and_invalid_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'prefs.json'
            self.assertEqual(load_preferences(path), {'port': 8765, 'theme': 'purple'})
            for value in ('broken', '[]', '{"port":false,"theme":[]}'):
                path.write_text(value)
                self.assertEqual(load_preferences(path), {'port': 8765, 'theme': 'purple'})

    def test_independent_updates_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'prefs.json'
            save_preferences({'port': 9876}, path)
            save_preferences({'theme': 'light'}, path)
            self.assertEqual(load_preferences(path), {'port': 9876, 'theme': 'light'})
            with self.assertRaises(ValueError):
                save_preferences({'port': 0}, path)
            self.assertEqual(json.loads(path.read_text())['port'], 9876)

    def test_saved_port_and_explicit_override(self):
        with patch('launch.load_preferences', return_value={'port': 9876, 'theme': 'purple'}):
            with patch('sys.argv', ['launch.py', '--status']), patch('launch.service_status', return_value={}) as status:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(launch.main(), 0)
                status.assert_called_once_with(9876)
            with patch('sys.argv', ['launch.py', '--status', '--port', '8765']), patch('launch.service_status', return_value={}) as status:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(launch.main(), 0)
                status.assert_called_once_with(8765)


if __name__ == '__main__':
    unittest.main()
