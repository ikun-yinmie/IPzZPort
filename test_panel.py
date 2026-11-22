import asyncio
import http.client
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from panel import Manager, PanelServer


def free_port(family=socket.AF_INET):
    host = '::1' if family == socket.AF_INET6 else '127.0.0.1'
    with socket.socket(family, socket.SOCK_STREAM) as listener:
        listener.bind((host, 0))
        return listener.getsockname()[1]


class PanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'state.json'
        self.manager = Manager(self.path, free_port(), 2)
        self.addAsyncCleanup(self.manager.close)

    def rule(self, port=None, enabled=True):
        port = port or free_port(socket.AF_INET6)
        return {
            'name': '测试规则', 'listen': f'[::1]:{port}',
            'target': '127.0.0.1:9632', 'enabled': enabled,
        }

    async def create(self, rule):
        rules = await self.manager.dispatch({'action': 'create', 'rule': rule})
        return rules[-1]['id']

    async def test_persistence_start_stop_and_delete(self):
        key = await self.create(self.rule())
        self.assertTrue(self.manager.snapshot()[0]['running'])
        await self.manager.close()
        restored = Manager(self.path, self.manager.control.port, 2)
        self.addAsyncCleanup(restored.close)
        await restored.restore()
        self.assertTrue(restored.snapshot()[0]['running'])
        self.assertIn(key, restored.active)
        await restored.dispatch({'action': 'stop', 'id': key})
        self.assertFalse(restored.snapshot()[0]['running'])
        self.assertFalse(json.loads(self.path.read_text())[key]['enabled'])
        await restored.dispatch({'action': 'start', 'id': key})
        self.assertTrue(restored.snapshot()[0]['running'])
        await restored.dispatch({'action': 'delete', 'id': key})
        self.assertEqual(restored.snapshot(), [])
        self.assertEqual(json.loads(self.path.read_text()), {})

    async def test_failed_binding_keeps_original_rule(self):
        key = await self.create(self.rule())
        original = self.manager.active[key]
        original_file = self.path.read_text()
        with socket.socket(socket.AF_INET6) as occupied:
            occupied.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            occupied.bind(('::1', 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            with self.assertRaises(OSError):
                await self.manager.dispatch({'action': 'update', 'id': key, 'rule': self.rule(port)})
        self.assertIs(self.manager.active[key], original)
        self.assertFalse(original.closing)
        self.assertEqual(self.path.read_text(), original_file)

    async def test_save_failure_restores_overlapping_rule(self):
        rule = self.rule()
        key = await self.create(rule)
        changed = dict(rule, target='127.0.0.1:9633')
        with patch.object(self.manager, 'save', side_effect=OSError('磁盘写入失败')):
            with self.assertRaisesRegex(OSError, '磁盘写入失败'):
                await self.manager.dispatch({'action': 'update', 'id': key, 'rule': changed})
        self.assertEqual(self.manager.entries[key].rule.target.port, 9632)
        self.assertFalse(self.manager.active[key].closing)

    async def test_duplicate_and_cycle_rejected(self):
        rule = self.rule(enabled=False)
        await self.create(rule)
        with self.assertRaisesRegex(ValueError, '重叠'):
            await self.create(rule)
        first, second = free_port(), free_port(socket.AF_INET6)
        await self.create({'name': '正向', 'listen': f'127.0.0.1:{first}', 'target': f'[::1]:{second}', 'enabled': False})
        with self.assertRaisesRegex(ValueError, '循环'):
            await self.create({'name': '反向', 'listen': f'[::1]:{second}', 'target': f'127.0.0.1:{first}', 'enabled': False})

    async def test_restoration_reports_occupied_port(self):
        port = free_port(socket.AF_INET6)
        await self.create(self.rule(port))
        await self.manager.close()
        with socket.socket(socket.AF_INET6) as occupied:
            occupied.bind(('::1', port))
            occupied.listen()
            restored = Manager(self.path, self.manager.control.port, 2)
            self.addAsyncCleanup(restored.close)
            with self.assertLogs('IPzZPort', 'WARNING'):
                await restored.restore()
            status = restored.snapshot()[0]
            self.assertFalse(status['running'])
            self.assertIn('无法监听', status['error'])

    async def test_http_host_origin_and_rule_submission(self):
        port = self.manager.control.port
        server = PanelServer(port, self.manager, asyncio.get_running_loop())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        async def close():
            await asyncio.to_thread(server.shutdown)
            thread.join()
            server.server_close()

        self.addAsyncCleanup(close)

        def request(method, path='/', headers=None, data=None):
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            try:
                connection.request(method, path, body=data, headers=headers or {})
                response = connection.getresponse()
                return response.status, response.read()
            finally:
                connection.close()

        self.assertEqual((await asyncio.to_thread(request, 'GET'))[0], 200)
        self.assertEqual((await asyncio.to_thread(request, 'GET', '/', {'Host': 'evil.example'}))[0], 403)
        body = json.dumps({'action': 'create', 'rule': self.rule(enabled=False)})
        headers = {'Content-Type': 'application/json'}
        self.assertEqual((await asyncio.to_thread(request, 'POST', '/api/rules', headers, body))[0], 403)
        headers['Origin'] = f'http://127.0.0.1:{port}'
        status, response = await asyncio.to_thread(request, 'POST', '/api/rules', headers, body)
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(response)['rules']), 1)
        headers['Origin'] = 'https://evil.example'
        self.assertEqual((await asyncio.to_thread(request, 'POST', '/api/rules', headers, body))[0], 403)


if __name__ == '__main__':
    unittest.main()
