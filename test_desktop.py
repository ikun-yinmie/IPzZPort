import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import threading
import time
import unittest
from unittest.mock import patch

from PyQt5 import QtCore, QtGui, QtWidgets
from desktop import Controller


STOPPED = {
    'running': False, 'managed': False, 'occupied': False, 'pid': None,
    'rules': None, 'listeners': None, 'connections': None, 'error': None,
}
RUNNING = dict(STOPPED, running=True, managed=True, pid=123, rules=1, listeners=1, connections=0)


class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.status = patch('desktop.service_status', return_value=RUNNING.copy())
        self.status.start()
        self.addCleanup(self.status.stop)
        self.window = Controller(19876)
        self.window.poll_timer.stop()
        self.window.tick_timer.stop()
        self.window.show()
        self.addCleanup(self.window.close)
        self.settle()

    def settle(self):
        deadline = time.monotonic() + 4
        while self.window.future is not None and time.monotonic() < deadline:
            self.app.processEvents()
            self.window.tick()
            time.sleep(.005)
        self.assertIsNone(self.window.future)

    def test_background_refresh_preserves_buttons_focus_and_unchanged_text(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        window = self.window
        window.stop_button.setFocus()
        self.app.processEvents()
        with patch.object(window, 'work', side_effect=lambda *args: (gate.wait(2), window.inspect(window.port))[1]):
            with patch.object(window.status_label, 'setText', wraps=window.status_label.setText) as status_text:
                with patch.object(window.logs, 'setPlainText', wraps=window.logs.setPlainText) as log_text:
                    window.refresh()
                    self.assertTrue(window.stop_button.isEnabled())
                    self.assertTrue(window.restart_button.isEnabled())
                    self.assertTrue(window.open_button.isEnabled())
                    self.assertTrue(window.stop_button.hasFocus())
                    gate.set()
                    self.settle()
                    status_text.assert_not_called()
                    log_text.assert_not_called()

    def test_actions_queued_during_poll_run_once(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        original = self.window.work

        def work(action, port, payload):
            if action == 'status':
                gate.wait(2)
            return original(action, port, payload)

        with patch.object(self.window, 'work', side_effect=work), patch('desktop.launch') as launch:
            self.window.refresh()
            self.window.stop_button.click()
            self.assertEqual(self.window.pending, ('stop', None))
            self.assertFalse(self.window.stop_button.isEnabled())
            gate.set()
            self.settle()
            launch.assert_called_once_with(19876, stop=True, browser=False)

    def test_incremental_logs_preserve_selection_and_scroll(self):
        window = self.window
        log = '\n'.join(f'日志 {index}' for index in range(300)) + '\n'
        window.update_log(log)
        scroll = window.logs.verticalScrollBar()
        cursor = window.logs.textCursor()
        cursor.setPosition(0)
        cursor.setPosition(4, QtGui.QTextCursor.KeepAnchor)
        window.logs.setTextCursor(cursor)
        scroll.setValue(8)
        before = scroll.value()
        with patch.object(window.logs, 'setPlainText', wraps=window.logs.setPlainText) as reset:
            window.update_log(log + '新增日志\n')
            reset.assert_not_called()
        self.assertEqual(scroll.value(), before)
        self.assertEqual(window.logs.textCursor().selectedText(), '日志 0')

    def test_themes_apply_without_changing_service(self):
        for theme in ('light', 'dark', 'purple'):
            self.window.apply_theme(theme)
            self.assertEqual(self.window.theme, theme)
            self.assertIn('border-radius: 18px', self.window.styleSheet())
            self.assertTrue(self.window.stop_button.isEnabled())


if __name__ == '__main__':
    unittest.main()
