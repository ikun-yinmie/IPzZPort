"""Local HTTP control panel for IPzZPort."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import signal
import tempfile
import threading
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ipzzport import Config, Endpoint, Forwarder, Rule


LOG = logging.getLogger('IPzZPort')
ROOT = Path(__file__).resolve().parent
MAX_BODY = 4096
MAX_RULES = 128
ASSETS = {
    '/': ('panel.html', 'text/html; charset=utf-8'),
    '/panel.css': ('panel.css', 'text/css; charset=utf-8'),
    '/panel.js': ('panel.js', 'text/javascript; charset=utf-8'),
}


@dataclass(frozen=True)
class Entry:
    name: str
    rule: Rule
    enabled: bool

    @classmethod
    def parse(cls, data: object) -> Entry:
        if not isinstance(data, dict) or data.keys() != {'name', 'listen', 'target', 'enabled'}:
            raise ValueError('规则须包含名称、监听地址、目标地址和启用状态')
        name = data['name']
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 64:
            raise ValueError('规则名称须为 1 到 64 个字符')
        if not isinstance(data['enabled'], bool):
            raise ValueError('启用状态须为布尔值')
        addresses = (data['listen'], data['target'])
        if any(not isinstance(addr, str) or len(addr) > 80 for addr in addresses):
            raise ValueError('地址须为 IP 字面量加端口')
        return cls(name.strip(), Rule.parse(*(addr.strip() for addr in addresses)), data['enabled'])

    def data(self) -> dict:
        return {
            'name': self.name, 'listen': str(self.rule.listen),
            'target': str(self.rule.target), 'enabled': self.enabled,
        }


def overlaps(first: Endpoint, second: Endpoint) -> bool:
    return first.family == second.family and first.port == second.port and (
        first == second or first.address.is_unspecified or second.address.is_unspecified
    )


class Manager:
    def __init__(self, path: Path, port: int, timeout: float) -> None:
        self.path = path
        self.timeout = timeout
        self.control = Endpoint.parse(f'127.0.0.1:{port}')
        self.entries: dict[str, Entry] = {}
        self.active: dict[str, Forwarder] = {}
        self.errors: dict[str, str] = {}
        self.lock = asyncio.Lock()
        self.closing = False

    def load(self) -> None:
        try:
            raw = self.path.read_text(encoding='utf-8')
        except FileNotFoundError:
            return
        data = json.loads(raw)
        if not isinstance(data, dict) or len(data) > MAX_RULES:
            raise ValueError('面板规则文件格式无效')
        self.entries = {key: Entry.parse(value) for key, value in data.items()}
        for key, entry in self.entries.items():
            self.validate(key, entry)

    def save(self, entries: dict[str, Entry]) -> None:
        temp = None
        try:
            with tempfile.NamedTemporaryFile(
                mode='w', encoding='utf-8', dir=self.path.parent,
                prefix=f'.{self.path.name}.', delete=False,
            ) as file:
                temp = Path(file.name)
                json.dump({key: value.data() for key, value in entries.items()}, file, ensure_ascii=False, indent=2)
                file.write('\n')
            os.replace(temp, self.path)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)

    def validate(self, key: str, entry: Entry) -> None:
        if overlaps(self.control, entry.rule.listen) or entry.rule.target == self.control:
            raise ValueError('规则不能使用管理面板地址')
        if any(key != other and overlaps(entry.rule.listen, saved.rule.listen) for other, saved in self.entries.items()):
            raise ValueError(f'监听地址 {entry.rule.listen} 与已保存的规则重叠')
        listeners = {saved.rule.listen: saved.rule.target for other, saved in self.entries.items() if other != key}
        next_address = entry.rule.target
        visited = {entry.rule.listen}
        while next_address in listeners:
            if next_address in visited:
                raise ValueError('转发规则形成循环')
            visited.add(next_address)
            next_address = listeners[next_address]
        if next_address in visited:
            raise ValueError('转发规则形成循环')

    async def start_rule(self, entry: Entry) -> Forwarder:
        forwarder = Forwarder(Config((entry.rule,), self.timeout))
        await forwarder.start()
        return forwarder

    async def restore(self) -> None:
        self.load()
        for key, entry in self.entries.items():
            if entry.enabled:
                try:
                    self.active[key] = await self.start_rule(entry)
                except OSError as error:
                    self.errors[key] = str(error)
                    LOG.warning('%s', error)

    def snapshot(self) -> list[dict]:
        return [
            dict(
                entry.data(), id=key, running=key in self.active,
                connections=len(self.active[key].connections) if key in self.active else 0,
                error=self.errors.get(key),
            )
            for key, entry in self.entries.items()
        ]

    async def dispatch(self, data: dict | None = None) -> list[dict]:
        async with self.lock:
            if self.closing:
                raise OSError('面板正在退出')
            if data is not None:
                await self.change(data)
            return self.snapshot()

    async def change(self, data: dict) -> None:
        action = data.get('action')
        key = data.get('id')
        if action == 'create':
            if len(self.entries) >= MAX_RULES:
                raise ValueError(f'最多保存 {MAX_RULES} 条规则')
            key = secrets.token_hex(12)
            entry = Entry.parse(data.get('rule'))
        else:
            if not isinstance(key, str) or key not in self.entries:
                raise ValueError('规则已不存在，请刷新页面')
            previous = self.entries[key]
            if action in ('start', 'stop'):
                entry = replace(previous, enabled=action == 'start')
            elif action == 'update':
                entry = Entry.parse(data.get('rule'))
            elif action == 'delete':
                entry = None
            else:
                raise ValueError('未知的管理操作')

        entries = self.entries.copy()
        if entry is None:
            del entries[key]
        else:
            self.validate(key, entry)
            entries[key] = entry

        old = self.active.get(key)
        desired = entry if entry is not None and entry.enabled else None
        current = old
        try:
            if desired is None:
                current = None
            elif old is None or old.config.rules[0] != desired.rule:
                # Overlapping bindings require releasing the old socket first.
                if old is not None and overlaps(old.config.rules[0].listen, desired.rule.listen):
                    await old.close()
                    del self.active[key]
                    current = None
                current = await self.start_rule(desired)
            self.save(entries)
        except OSError:
            if current is not None and current is not old:
                await current.close()
            if old is not None and key not in self.active:
                try:
                    self.active[key] = await self.start_rule(self.entries[key])
                except OSError as restore_error:
                    self.errors[key] = str(restore_error)
                    LOG.error('恢复规则失败：%s', restore_error)
            raise

        self.entries = entries
        if old is not None and old is not current and not old.closing:
            await old.close()
        if current is None:
            self.active.pop(key, None)
        else:
            self.active[key] = current
        self.errors.pop(key, None)

    async def close(self) -> None:
        async with self.lock:
            self.closing = True
            await asyncio.gather(*(forwarder.close() for forwarder in self.active.values()))
            self.active.clear()


class Handler(BaseHTTPRequestHandler):
    def setup(self) -> None:
        self.request.settimeout(4)
        super().setup()

    def log_message(self, format: str, *args) -> None:
        LOG.debug(format, *args)

    def respond(self, code: int, content: bytes, mime: str) -> None:
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-IPzZPort', 'local-panel')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(content)

    def json_response(self, code: int, data: dict) -> None:
        self.respond(code, json.dumps(data, ensure_ascii=False).encode(), 'application/json; charset=utf-8')

    def permitted(self, write: bool = False) -> bool:
        origin = f'http://{self.server.authority}'
        if self.headers.get_all('Host') != [self.server.authority]:
            self.json_response(403, {'error': '请使用启动日志中的本机面板地址'})
            return False
        if write and self.headers.get_all('Origin') != [origin]:
            self.json_response(403, {'error': '管理操作须从本机面板提交'})
            return False
        return True

    def do_GET(self) -> None:
        if not self.permitted():
            return
        if self.path == '/api/rules':
            self.api()
        elif self.path in ASSETS:
            file, mime = ASSETS[self.path]
            self.respond(200, (ROOT / file).read_bytes(), mime)
        else:
            self.json_response(404, {'error': '页面不存在'})

    def do_POST(self) -> None:
        if not self.permitted(write=True):
            return
        if self.path != '/api/rules':
            self.json_response(404, {'error': '接口不存在'})
            return
        if self.headers.get_content_type() != 'application/json':
            self.json_response(415, {'error': '请求须使用 JSON'})
            return
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) != 1 or self.headers.get('Transfer-Encoding') is not None:
            self.json_response(400, {'error': '请求长度无效'})
            return
        length = lengths[0]
        if not length.isascii() or not length.isdecimal() or len(length) > 4 or not 1 <= int(length) <= MAX_BODY:
            self.json_response(400, {'error': '请求长度无效'})
            return
        try:
            data = json.loads(self.rfile.read(int(length)))
            if not isinstance(data, dict):
                raise ValueError
        except (ValueError, RecursionError):
            self.json_response(400, {'error': '请求须为 JSON 对象'})
            return
        self.api(data)

    def api(self, data: dict | None = None) -> None:
        response = asyncio.run_coroutine_threadsafe(self.server.manager.dispatch(data), self.server.loop)
        try:
            rules = response.result()
        except ValueError as error:
            self.json_response(400, {'error': str(error)})
        except OSError as error:
            self.json_response(409, {'error': str(error)})
        else:
            self.json_response(200, {'rules': rules})


class PanelServer(ThreadingHTTPServer):
    def __init__(self, port: int, manager: Manager, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__(('127.0.0.1', port), Handler)
        self.authority = f'127.0.0.1:{port}'
        self.manager = manager
        self.loop = loop


async def serve_panel(port: int, path: Path, timeout: float) -> None:
    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()
    try:
        loop.add_signal_handler(signal.SIGTERM, stopped.set)
    except NotImplementedError:
        pass
    manager = Manager(path, port, timeout)
    with PanelServer(port, manager, loop) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        try:
            await manager.restore()
            thread.start()
            LOG.info('Web 管理面板：http://%s/', server.authority)
            await stopped.wait()
        finally:
            if thread.is_alive():
                await asyncio.to_thread(server.shutdown)
                thread.join()
            await manager.close()


def run_panel(port: int, path: Path, timeout: float) -> int:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        asyncio.run(serve_panel(port, path, timeout))
    except KeyboardInterrupt:
        LOG.info('已停止')
    except (ValueError, OSError) as error:
        LOG.error('面板启动失败：%s', error)
        return 1
    return 0
