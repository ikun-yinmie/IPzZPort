#!/usr/bin/env python3
"""Linux desktop launcher for the local IPzZPort panel."""

import argparse
import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from preferences import load_preferences


ROOT = Path(__file__).resolve().parent


def is_panel(port):
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(f'http://127.0.0.1:{port}/', timeout=1) as response:
            return response.headers.get('X-IPzZPort') == 'local-panel'
    except (URLError, TimeoutError, OSError):
        return False


def occupied(port):
    with socket.socket() as connection:
        return connection.connect_ex(('127.0.0.1', port)) == 0


def process_stamp(pid):
    try:
        data = Path(f'/proc/{pid}/stat').read_text()
        return data[data.rfind(')') + 2:].split()[19]
    except (OSError, IndexError):
        return None


def owned_process(record, port):
    try:
        data = json.loads(record.read_text())
        pid = data['pid']
        if type(pid) is not int or pid <= 1 or data['stamp'] is None:
            return None
        if process_stamp(pid) != data['stamp']:
            return None
        arguments = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        expected = [os.fsencode(ROOT / 'ipzzport.py'), b'--web', b'--web-port', str(port).encode()]
        if arguments[2:6] != expected:
            return None
        return pid
    except (OSError, ValueError, KeyError, TypeError):
        return None


def open_panel(port):
    try:
        subprocess.Popen(
            ['xdg-open', f'http://127.0.0.1:{port}/'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError as error:
        raise OSError(f'面板已运行，请手动打开 http://127.0.0.1:{port}/；浏览器启动失败：{error}') from error


def launch(port=8765, stop=False, browser=True):
    runtime = ROOT / '.runtime'
    runtime.mkdir(exist_ok=True)
    record = runtime / f'panel-{port}.json'
    log = runtime / f'panel-{port}.log'
    with (runtime / f'panel-{port}.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if stop:
            pid = owned_process(record, port)
            if pid is None:
                if occupied(port):
                    raise OSError('此端口的服务不是由本启动器管理，请在原终端停止。')
                record.unlink(missing_ok=True)
                print('面板已停止')
                return
            os.kill(pid, signal.SIGTERM)
            for _ in range(60):
                if not occupied(port):
                    record.unlink(missing_ok=True)
                    print('面板和转发已停止')
                    return
                time.sleep(.1)
            raise OSError(f'停止尚未完成，请查看日志：{log}')

        if not is_panel(port):
            if occupied(port):
                raise OSError(f'端口 {port} 已被其他服务占用，请使用 --port 指定空闲端口。')
            with log.open('ab') as output:
                process = subprocess.Popen(
                    [sys.executable, '-B', str(ROOT / 'ipzzport.py'), '--web', '--web-port', str(port)],
                    cwd=ROOT, stdin=subprocess.DEVNULL, stdout=output,
                    stderr=subprocess.STDOUT, start_new_session=True,
                )
            record.write_text(json.dumps({'pid': process.pid, 'stamp': process_stamp(process.pid)}))
            for _ in range(60):
                if process.poll() is not None:
                    record.unlink(missing_ok=True)
                    raise OSError(f'启动失败，请查看日志：{log}')
                if is_panel(port):
                    break
                time.sleep(.1)
            else:
                process.terminate()
                process.wait(timeout=5)
                record.unlink(missing_ok=True)
                raise OSError(f'面板启动超时，请查看日志：{log}')
        print(f'面板已运行：http://127.0.0.1:{port}/')
        if browser:
            open_panel(port)


def service_status(port=8765):
    record = ROOT / '.runtime' / f'panel-{port}.json'
    pid = owned_process(record, port)
    running = is_panel(port)
    result = {
        'running': running, 'managed': pid is not None, 'pid': pid,
        'occupied': False, 'rules': None, 'listeners': None, 'connections': None,
        'error': None,
    }
    if running:
        opener = build_opener(ProxyHandler({}))
        try:
            with opener.open(f'http://127.0.0.1:{port}/api/rules', timeout=1) as response:
                rules = json.load(response)['rules']
            result.update(
                rules=len(rules), listeners=sum(rule['running'] for rule in rules),
                connections=sum(rule['connections'] for rule in rules),
            )
        except (URLError, OSError, ValueError, KeyError, TypeError) as error:
            result['error'] = f'读取转发状态失败：{error}'
    else:
        result['occupied'] = occupied(port)
    return result


def restart(port=8765):
    launch(port, stop=True, browser=False)
    launch(port, browser=False)


def report_error(message):
    print(message, file=sys.stderr)
    try:
        subprocess.Popen(
            ['zenity', '--error', '--title=IPzZPort', '--no-markup', '--text', message],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError:
        pass


def main():
    parser = argparse.ArgumentParser(description='IPzZPort 桌面服务控制面板')
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--start', action='store_true', help='直接启动服务')
    actions.add_argument('--stop', action='store_true', help='停止服务及全部转发规则')
    actions.add_argument('--restart', action='store_true', help='重启服务并恢复启用的规则')
    actions.add_argument('--status', action='store_true', help='显示服务状态（JSON）')
    parser.add_argument('--port', type=int, help='本机面板端口，默认使用已保存设置（初始 8765）')
    parser.add_argument('--no-browser', action='store_true', help='直接启动，不打开浏览器（兼容原启动方式）')
    args = parser.parse_args()
    if args.port is None:
        args.port = load_preferences()['port']
    if not 1 <= args.port <= 65535:
        parser.error('端口须在 1 到 65535 之间')
    try:
        if args.status:
            print(json.dumps(service_status(args.port), ensure_ascii=False))
        elif args.restart:
            restart(args.port)
        elif args.stop or args.start or args.no_browser:
            launch(args.port, args.stop, not args.no_browser and not args.stop)
        else:
            from desktop import show_controller
            return show_controller(args.port)
    except OSError as error:
        report_error(str(error))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
