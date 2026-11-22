import asyncio
import io
import socket
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from ipzzport import Config, Endpoint, Forwarder, Rule, load_config, main


class ConfigTests(unittest.TestCase):
    def test_addresses_and_ports(self):
        self.assertEqual(str(Endpoint.parse('[0:0:0:0:0:0:0:1]:8080')), '[::1]:8080')
        self.assertEqual(Endpoint.parse('127.0.0.1:443').family, socket.AF_INET)
        for endpoint in ('::1:80', '[127.0.0.1]:80', 'localhost:80', '127.0.0.1:0', '[::1]:65536'):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                Endpoint.parse(endpoint)
        for target in ('[::]:8080', '[::1]:8080'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                Rule.parse('[::]:8080', target)

    def test_toml_and_cli_validation(self):
        example = Path(__file__).with_name('config.example.toml')
        self.assertEqual(len(load_config(example).rules), 2)
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['--config', str(example), '--check']), 0)
        self.assertIn('2 条', output.getvalue())
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
            main(['--listen', '[::1]:8080', '--target', '127.0.0.1:8080', '--connect-timeout', 'nan'])
        self.assertEqual(result.exception.code, 2)

    def test_duplicate_listener(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            path.write_text(
                '[[rules]]\nlisten = "[::1]:8080"\ntarget = "127.0.0.1:8080"\n' * 2,
                encoding='utf-8',
            )
            with self.assertRaisesRegex(ValueError, '规则 2.*重复'):
                load_config(path)


class ForwardingTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def unused_port(host, family):
        with socket.socket(family, socket.SOCK_STREAM) as reservation:
            reservation.bind((host, 0))
            return reservation.getsockname()[1]

    async def backend(self, host, family, name):
        async def serve(reader, writer):
            try:
                payload = await reader.read()
                writer.write(name + payload)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(serve, host, 0, family=family)

        async def close():
            server.close()
            await server.wait_closed()

        self.addAsyncCleanup(close)
        return server.sockets[0].getsockname()[1]

    async def forward(self, *rules):
        forwarder = Forwarder(Config(rules))
        self.addAsyncCleanup(forwarder.close)
        await forwarder.start()
        return forwarder

    async def roundtrip(self, host, port, family, expected):
        payload = bytes(range(256)) * 1024
        async with asyncio.timeout(5):
            reader, writer = await asyncio.open_connection(host, port, family=family)
            try:
                writer.write(payload)
                await writer.drain()
                writer.write_eof()
                self.assertEqual(await reader.read(), expected + payload)
            finally:
                writer.close()
                await writer.wait_closed()

    async def test_bidirectional_address_families_and_port_mapping(self):
        backend4 = await self.backend('127.0.0.1', socket.AF_INET, b'IPv4:')
        backend6 = await self.backend('::1', socket.AF_INET6, b'IPv6:')
        port4 = self.unused_port('127.0.0.1', socket.AF_INET)
        port6 = self.unused_port('::1', socket.AF_INET6)
        await self.forward(
            Rule.parse(f'[::1]:{port6}', f'127.0.0.1:{backend4}'),
            Rule.parse(f'127.0.0.1:{port4}', f'[::1]:{backend6}'),
        )
        await asyncio.gather(
            self.roundtrip('::1', port6, socket.AF_INET6, b'IPv4:'),
            self.roundtrip('127.0.0.1', port4, socket.AF_INET, b'IPv6:'),
        )

    async def test_same_port_coexists_with_ipv4_service(self):
        port = await self.backend('127.0.0.1', socket.AF_INET, b'IPv4:')
        forwarder = await self.forward(Rule.parse(f'[::]:{port}', f'127.0.0.1:{port}'))
        listener = forwarder.servers[0].sockets[0]
        self.assertEqual(listener.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY), 1)
        await self.roundtrip('127.0.0.1', port, socket.AF_INET, b'IPv4:')
        await self.roundtrip('::1', port, socket.AF_INET6, b'IPv4:')

    async def test_bind_failure_releases_previous_listener(self):
        occupied = await self.backend('127.0.0.1', socket.AF_INET, b'')
        port = self.unused_port('127.0.0.1', socket.AF_INET)
        config = Config((
            Rule.parse(f'127.0.0.1:{port}', '[::1]:9632'),
            Rule.parse(f'127.0.0.1:{occupied}', '[::1]:9632'),
        ))
        forwarder = Forwarder(config)
        self.addAsyncCleanup(forwarder.close)
        with self.assertRaisesRegex(OSError, f'无法监听 127.0.0.1:{occupied}'):
            await forwarder.start()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(('127.0.0.1', port))

    async def test_shutdown_with_active_connections(self):
        backend = await self.backend('127.0.0.1', socket.AF_INET, b'')
        port = self.unused_port('::1', socket.AF_INET6)
        forwarder = await self.forward(Rule.parse(f'[::1]:{port}', f'127.0.0.1:{backend}'))
        async with asyncio.timeout(5):
            readers_and_writers = await asyncio.gather(
                *(asyncio.open_connection('::1', port) for _ in range(10))
            )
            self.assertTrue(forwarder.connections)
            try:
                await forwarder.close()
                for reader, _ in readers_and_writers:
                    self.assertEqual(await reader.read(), b'')
            finally:
                for _, writer in readers_and_writers:
                    writer.close()
                    await writer.wait_closed()


if __name__ == '__main__':
    unittest.main()
