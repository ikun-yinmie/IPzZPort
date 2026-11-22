#!/usr/bin/env python3
"""IPzZPort: forward TCP connections to fixed IPv4 or IPv6 endpoints."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import logging
import math
import signal
import socket
import tomllib
from dataclasses import dataclass
from pathlib import Path


LOG = logging.getLogger('IPzZPort')
COPY_SIZE = 64 * 1024
DEFAULT_CONNECT_TIMEOUT = 5.0


@dataclass(frozen=True)
class Endpoint:
    address: ipaddress.IPv4Address | ipaddress.IPv6Address
    port: int

    @classmethod
    def parse(cls, text: str) -> Endpoint:
        if not isinstance(text, str):
            raise ValueError('地址须为字符串')
        host, separator, port = text.rpartition(':')
        bracketed = host.startswith('[') and host.endswith(']')
        if bracketed:
            host = host[1:-1]
        if not separator or not (port.isascii() and port.isdecimal()):
            raise ValueError(f'无效地址 {text!r}，请使用 IPv4:端口 或 [IPv6]:端口')
        if len(port) > 5 or not 1 <= int(port) <= 65535:
            raise ValueError(f'端口须在 1 到 65535 之间（{text!r}）')
        try:
            address = ipaddress.ip_address(host)
        except ValueError as error:
            raise ValueError(f'无效 IP 地址 {host!r}') from error
        if bracketed != (address.version == 6):
            raise ValueError(f'IPv6 地址须带方括号，IPv4 地址直接填写（{text!r}）')
        if isinstance(address, ipaddress.IPv6Address) and (
            address.scope_id is not None or address.ipv4_mapped is not None
        ):
            raise ValueError(f'请使用无区域标识的原生 IPv4 或 IPv6 地址（{text!r}）')
        return cls(address, int(port))

    @property
    def family(self) -> socket.AddressFamily:
        return socket.AF_INET6 if self.address.version == 6 else socket.AF_INET

    def __str__(self) -> str:
        host = f'[{self.address}]' if self.address.version == 6 else str(self.address)
        return f'{host}:{self.port}'


@dataclass(frozen=True)
class Rule:
    listen: Endpoint
    target: Endpoint

    @classmethod
    def parse(cls, listen: str, target: str) -> Rule:
        rule = cls(Endpoint.parse(listen), Endpoint.parse(target))
        if rule.target.address.is_unspecified:
            raise ValueError('目标须为具体 IP 地址')
        if rule.listen == rule.target or (
            rule.listen.address.is_unspecified
            and rule.target.address.is_loopback
            and rule.listen.family == rule.target.family
            and rule.listen.port == rule.target.port
        ):
            raise ValueError('监听地址与目标地址形成自转发')
        return rule


@dataclass(frozen=True)
class Config:
    rules: tuple[Rule, ...]
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT


def parse_timeout(value: int | float) -> float:
    if type(value) not in (int, float):
        raise ValueError('连接超时须为正数（单位为秒）')
    try:
        timeout = float(value)
    except OverflowError as error:
        raise ValueError('连接超时超出可用范围') from error
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('连接超时须为有限正数（单位为秒）')
    return timeout


def load_config(path: Path) -> Config:
    with path.open('rb') as file:
        data = tomllib.load(file)
    unknown = data.keys() - {'connect_timeout', 'rules'}
    if unknown:
        raise ValueError(f'未知配置项：{", ".join(sorted(unknown))}')
    items = data.get('rules')
    if not isinstance(items, list) or not items:
        raise ValueError('至少配置一条 [[rules]] 转发规则')

    rules = []
    listeners = set()
    for index, item in enumerate(items, 1):
        try:
            if not isinstance(item, dict) or item.keys() != {'listen', 'target'}:
                raise ValueError('每条规则须包含 listen 和 target')
            rule = Rule.parse(item['listen'], item['target'])
            if rule.listen in listeners:
                raise ValueError(f'重复的监听地址 {rule.listen}')
        except ValueError as error:
            raise ValueError(f'规则 {index}：{error}') from error
        rules.append(rule)
        listeners.add(rule.listen)
    timeout = parse_timeout(data.get('connect_timeout', DEFAULT_CONNECT_TIMEOUT))
    return Config(tuple(rules), timeout)


def make_listener(endpoint: Endpoint) -> socket.socket:
    listener = socket.socket(endpoint.family, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if endpoint.family == socket.AF_INET6:
            listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        listener.bind((str(endpoint.address), endpoint.port))
    except BaseException:
        listener.close()
        raise
    return listener


async def copy_stream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while chunk := await reader.read(COPY_SIZE):
        writer.write(chunk)
        await writer.drain()
    writer.write_eof()


async def close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), timeout=1)
    except TimeoutError:
        writer.transport.abort()
    except OSError:
        pass


class Forwarder:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.servers: list[asyncio.Server] = []
        self.connections: dict[asyncio.Task, asyncio.StreamWriter] = {}
        self.closing = False

    async def start(self) -> None:
        try:
            for rule in self.config.rules:
                listener = make_listener(rule.listen)
                try:
                    server = await asyncio.start_server(
                        lambda reader, writer, selected=rule: self.accept(selected, reader, writer),
                        sock=listener,
                        start_serving=False,
                    )
                except BaseException:
                    listener.close()
                    raise
                self.servers.append(server)
            for rule, server in zip(self.config.rules, self.servers, strict=True):
                await server.start_serving()
                LOG.info('%s → %s', rule.listen, rule.target)
        except OSError as error:
            await self.close()
            raise OSError(f'无法监听 {rule.listen}：{error.strerror or error}') from error

    def accept(
        self, rule: Rule, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if self.closing:
            writer.close()
            return
        local = writer.get_extra_info('sockname')
        if rule.listen.address.is_unspecified and rule.target.port == local[1]:
            if rule.target.address == ipaddress.ip_address(local[0].split('%', 1)[0]):
                LOG.warning('%s → %s 形成自转发，已断开', rule.listen, rule.target)
                writer.close()
                return
        task = asyncio.create_task(self.relay(rule, reader, writer))
        self.connections[task] = writer
        task.add_done_callback(self.connections.pop)

    async def relay(
        self, rule: Rule, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        writers = [client_writer]
        tasks = []
        try:
            try:
                async with asyncio.timeout(self.config.connect_timeout):
                    target_reader, target_writer = await asyncio.open_connection(
                        str(rule.target.address), rule.target.port, family=rule.target.family
                    )
            except TimeoutError:
                LOG.warning('%s → %s 连接后端超时', rule.listen, rule.target)
                return
            except OSError as error:
                LOG.warning('%s → %s 连接后端失败：%s', rule.listen, rule.target, error)
                return
            writers.append(target_writer)
            tasks = [
                asyncio.create_task(copy_stream(client_reader, target_writer)),
                asyncio.create_task(copy_stream(target_reader, client_writer)),
            ]
            await asyncio.gather(*tasks)
        except OSError as error:
            LOG.debug('%s → %s 的连接结束：%s', rule.listen, rule.target, error)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(*(close_writer(writer) for writer in writers))

    async def close(self) -> None:
        self.closing = True
        for server in self.servers:
            server.close()
        connections = tuple(self.connections.items())
        for task, writer in connections:
            task.cancel()
            writer.close()
        await asyncio.gather(*(task for task, _ in connections), return_exceptions=True)
        await asyncio.gather(*(server.wait_closed() for server in self.servers))
        self.servers.clear()


async def run(config: Config) -> None:
    forwarder = Forwarder(config)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, stopped.set)
    except NotImplementedError:
        pass
    try:
        await forwarder.start()
        await stopped.wait()
    finally:
        await forwarder.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='IPzZPort 固定目标 TCP 端口转发')
    parser.add_argument('--config', type=Path, help='TOML 多规则配置文件')
    parser.add_argument('--listen', help='监听地址，如 [::1]:19632')
    parser.add_argument('--target', help='目标地址，如 127.0.0.1:9632')
    parser.add_argument('--connect-timeout', type=float, metavar='秒', help='覆盖后端连接超时')
    parser.add_argument('--check', action='store_true', help='仅校验规则和参数')
    parser.add_argument('--web', action='store_true', help='启动本机 Web 管理面板')
    parser.add_argument('--web-port', type=int, help='面板端口，默认 8765')
    parser.add_argument('--web-state', type=Path, help='面板规则文件，默认项目目录下的 panel-state.json')
    args = parser.parse_args(argv)

    if args.web:
        if any((args.config is not None, args.listen is not None, args.target is not None, args.check)):
            parser.error('--web 使用独立的规则文件，不能与 --config、--listen、--target 或 --check 同用')
        port = 8765 if args.web_port is None else args.web_port
        if not 1 <= port <= 65535:
            parser.error('--web-port 须在 1 到 65535 之间')
        try:
            timeout = DEFAULT_CONNECT_TIMEOUT if args.connect_timeout is None else parse_timeout(args.connect_timeout)
        except ValueError as error:
            parser.error(str(error))
        from panel import run_panel

        state = args.web_state if args.web_state is not None else Path(__file__).resolve().with_name('panel-state.json')
        return run_panel(port, state, timeout)

    if args.web_port is not None or args.web_state is not None:
        parser.error('--web-port 和 --web-state 需要 --web')

    try:
        if args.config is not None:
            if args.listen is not None or args.target is not None:
                parser.error('--config 不能与 --listen 或 --target 同时使用')
            config = load_config(args.config)
        else:
            if args.listen is None or args.target is None:
                parser.error('请提供 --config，或同时提供 --listen 和 --target')
            config = Config((Rule.parse(args.listen, args.target),))
        if args.connect_timeout is not None:
            config = Config(config.rules, parse_timeout(args.connect_timeout))
    except (OSError, ValueError) as error:
        parser.error(str(error))

    if args.check:
        print(f'配置有效，共 {len(config.rules)} 条 TCP 规则')
        return 0

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        LOG.info('已停止')
    except OSError as error:
        LOG.error('%s', error)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
