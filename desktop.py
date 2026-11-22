"""Rounded native controls with quiet, incremental status polling."""

from concurrent.futures import ThreadPoolExecutor

from PyQt5 import QtCore, QtGui, QtWidgets

from launch import ROOT, launch, open_panel, restart, service_status
from preferences import THEME_LABELS, load_preferences, parse_port, save_preferences


PALETTES = {
    'purple': {
        'bg': '#130e21', 'surface': '#231832', 'ink': '#f2eaff', 'muted': '#baaaca',
        'accent': '#c5a2ff', 'accent_hover': '#d9bdff', 'accent_ink': '#29163e',
        'button': '#342244', 'hover': '#49305d', 'border': '#59416d',
        'input': '#1a1128', 'disabled': '#867393', 'error': '#ffacc8', 'warn': '#e0bbff',
    },
    'dark': {
        'bg': '#0e1118', 'surface': '#1b202d', 'ink': '#edf0f7', 'muted': '#adb6ca',
        'accent': '#b7a7ef', 'accent_hover': '#d2c4ff', 'accent_ink': '#201932',
        'button': '#2b3142', 'hover': '#3b4358', 'border': '#4b556b',
        'input': '#131824', 'disabled': '#828ca0', 'error': '#ffa8be', 'warn': '#ddc3fa',
    },
    'light': {
        'bg': '#f5f2fa', 'surface': '#ffffff', 'ink': '#30243f', 'muted': '#655772',
        'accent': '#713ab5', 'accent_hover': '#8752c8', 'accent_ink': '#ffffff',
        'button': '#eee6f5', 'hover': '#e2d5ee', 'border': '#baabc8',
        'input': '#fcfaff', 'disabled': '#83768f', 'error': '#a32350', 'warn': '#714a96',
    },
}


def set_text(widget, value):
    if widget.text() != value:
        widget.setText(value)


def set_enabled(widget, enabled):
    if widget.isEnabled() != enabled:
        widget.setEnabled(enabled)


class Controller(QtWidgets.QWidget):
    def __init__(self, port=8765):
        super().__init__()
        self.port = port
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.future = None
        self.operation = None
        self.pending = None
        self.closed = False
        self.last_status = None
        self.log_text = None
        self.theme = load_preferences()['theme']
        self.message_role = 'muted'
        self.status_role = 'warn'
        self.setObjectName('controller')
        self.setWindowTitle('IPzZPort · 服务控制台')
        self.setWindowIcon(QtGui.QIcon(str(ROOT / 'ipzzport.svg')))
        self.resize(860, 740)
        self.setMinimumSize(780, 650)
        self.build()
        self.apply_theme(self.theme)
        self.tick_timer = QtCore.QTimer(self)
        self.tick_timer.timeout.connect(self.tick)
        self.tick_timer.start(80)
        self.poll_timer = QtCore.QTimer(self)
        self.poll_timer.timeout.connect(self.refresh)
        self.poll_timer.start(2000)
        self.refresh()

    @staticmethod
    def label(text, name=None):
        widget = QtWidgets.QLabel(text)
        if name:
            widget.setObjectName(name)
        return widget

    @staticmethod
    def button(text, handler, name=None):
        widget = QtWidgets.QPushButton(text)
        widget.setCursor(QtCore.Qt.PointingHandCursor)
        if name:
            widget.setObjectName(name)
        widget.clicked.connect(handler)
        return widget

    def build(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 20)
        layout.setSpacing(18)
        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QVBoxLayout()
        title.setSpacing(5)
        title.addWidget(self.label('IPzZPort  /  服务控制台', 'title'))
        title.addWidget(self.label('SERVICE CONTROL  ·  IPv4 ⇄ IPv6', 'subtitle'))
        header.addLayout(title)
        header.addStretch()
        theme = QtWidgets.QVBoxLayout()
        theme.addWidget(self.label('界面主题', 'muted'))
        self.theme_menu = QtWidgets.QComboBox()
        for key, name in THEME_LABELS.items():
            self.theme_menu.addItem(name, key)
        self.theme_menu.setAccessibleName('界面主题')
        self.theme_menu.setMinimumWidth(115)
        self.theme_menu.currentIndexChanged.connect(self.select_theme)
        theme.addWidget(self.theme_menu)
        header.addLayout(theme)
        layout.addLayout(header)

        settings = QtWidgets.QHBoxLayout()
        settings.addWidget(self.label('Web 端口', 'muted'))
        self.port_entry = QtWidgets.QLineEdit(str(self.port))
        self.port_entry.setAccessibleName('Web 端口')
        self.port_entry.setMaximumWidth(110)
        self.port_entry.setMaxLength(5)
        self.port_entry.returnPressed.connect(self.apply_port)
        settings.addWidget(self.port_entry)
        self.apply_button = self.button('应用端口', self.apply_port)
        settings.addWidget(self.apply_button)
        settings.addWidget(self.label('停止服务后可修改', 'muted'))
        settings.addStretch()
        layout.addLayout(settings)

        self.status_frame = QtWidgets.QFrame()
        self.status_frame.setObjectName('status-card')
        status = QtWidgets.QVBoxLayout(self.status_frame)
        status.setContentsMargins(22, 20, 22, 20)
        status.setSpacing(9)
        self.status_label = self.label('● 正在检查状态…', 'status')
        self.address_label = self.label(f'管理地址  http://127.0.0.1:{self.port}/', 'muted')
        self.address_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.detail_label = self.label('正在读取进程和转发状态', 'muted')
        for widget in (self.status_label, self.address_label, self.detail_label):
            status.addWidget(widget)
        layout.addWidget(self.status_frame)

        actions = QtWidgets.QHBoxLayout()
        actions.setSpacing(10)
        self.start_button = self.button('启动', lambda: self.execute('start'), 'primary')
        self.stop_button = self.button('停止', lambda: self.execute('stop'))
        self.restart_button = self.button('重启', lambda: self.execute('restart'))
        self.open_button = self.button('打开 Web 面板', self.open_web)
        self.refresh_button = self.button('刷新状态', self.refresh)
        for widget in (self.start_button, self.stop_button, self.restart_button, self.open_button):
            actions.addWidget(widget)
        actions.addStretch()
        actions.addWidget(self.refresh_button)
        layout.addLayout(actions)

        self.message_label = self.label('状态每 2 秒静默刷新。', 'message')
        self.message_label.setWordWrap(True)
        self.message_label.setMinimumHeight(32)
        layout.addWidget(self.message_label)
        logs_header = QtWidgets.QHBoxLayout()
        logs_header.addWidget(self.label('运行日志', 'logs-title'))
        logs_header.addStretch()
        logs_header.addWidget(self.label('仅在内容变化时更新 · 最近 24 KB', 'subtitle'))
        layout.addLayout(logs_header)
        self.logs = QtWidgets.QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setAccessibleName('运行日志')
        self.logs.setMinimumHeight(140)
        self.logs.setLineWrapMode(QtWidgets.QPlainTextEdit.WidgetWidth)
        self.logs.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.logs.customContextMenuRequested.connect(self.show_log_menu)
        layout.addWidget(self.logs, 1)
        layout.addWidget(self.label('关闭此窗口不会停止服务；停止服务会断开全部转发连接。', 'subtitle'))
        self.update_controls()

    def tint(self, widget, role):
        palette = widget.palette()
        color = QtGui.QColor(PALETTES[self.theme][role])
        if palette.color(QtGui.QPalette.WindowText) != color:
            palette.setColor(QtGui.QPalette.WindowText, color)
            widget.setPalette(palette)

    def message(self, text, role='muted'):
        self.message_role = role
        set_text(self.message_label, text)
        self.tint(self.message_label, role)

    def apply_theme(self, theme):
        self.theme = theme
        c = PALETTES[theme]
        self.setStyleSheet(f'''
            QWidget {{ color: {c['ink']}; font-family: "Noto Sans CJK SC"; font-size: 13px; }}
            QWidget#controller {{ background: {c['bg']}; }}
            QLabel {{ background: transparent; }}
            QLabel#title {{ color: {c['accent']}; font-size: 25px; font-weight: 600; }}
            QLabel#subtitle {{ color: {c['muted']}; font-size: 11px; }}
            QLabel#muted {{ color: {c['muted']}; }}
            QLabel#logs-title {{ color: {c['accent']}; font-size: 14px; font-weight: 600; }}
            QLabel#status {{ font-size: 19px; font-weight: 600; }}
            QFrame#status-card {{ background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 18px; }}
            QPushButton {{ background: {c['button']}; color: {c['ink']}; border: 1px solid {c['border']}; border-radius: 12px; padding: 10px 16px; min-height: 20px; }}
            QPushButton:hover {{ background: {c['hover']}; border-color: {c['accent']}; }}
            QPushButton:pressed {{ background: {c['surface']}; }}
            QPushButton:focus {{ border: 1px solid {c['accent']}; }}
            QPushButton#primary {{ background: {c['accent']}; color: {c['accent_ink']}; border-color: {c['accent']}; }}
            QPushButton#primary:hover {{ background: {c['accent_hover']}; }}
            QPushButton:disabled, QPushButton#primary:disabled {{ background: {c['surface']}; color: {c['disabled']}; border-color: {c['border']}; }}
            QLineEdit, QComboBox {{ background: {c['input']}; border: 1px solid {c['border']}; border-radius: 11px; padding: 9px 12px; selection-background-color: {c['accent']}; selection-color: {c['accent_ink']}; }}
            QLineEdit:focus, QComboBox:focus {{ border-color: {c['accent']}; }}
            QLineEdit:disabled {{ color: {c['disabled']}; background: {c['surface']}; }}
            QComboBox {{ padding-right: 28px; }}
            QComboBox::drop-down {{ border: none; width: 24px; }}
            QComboBox QAbstractItemView {{ background: {c['surface']}; color: {c['ink']}; border: 1px solid {c['border']}; border-radius: 8px; padding: 5px; selection-background-color: {c['hover']}; outline: none; }}
            QPlainTextEdit {{ background: {c['input']}; color: {c['ink']}; border: 1px solid {c['border']}; border-radius: 15px; padding: 12px; font-family: "DejaVu Sans Mono"; font-size: 12px; selection-background-color: {c['hover']}; }}
            QScrollBar:vertical {{ background: transparent; width: 10px; margin: 6px 2px; }}
            QScrollBar::handle:vertical {{ background: {c['border']}; border-radius: 3px; min-height: 28px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
        ''')
        blocked = self.theme_menu.blockSignals(True)
        self.theme_menu.setCurrentIndex(self.theme_menu.findData(theme))
        self.theme_menu.blockSignals(blocked)
        self.tint(self.status_label, self.status_role)
        self.tint(self.message_label, self.message_role)

    def select_theme(self, index):
        theme = self.theme_menu.itemData(index)
        try:
            save_preferences({'theme': theme})
        except (OSError, ValueError) as error:
            self.apply_theme(self.theme)
            self.message(f'主题保存失败：{error}', 'error')
            return
        self.apply_theme(theme)
        self.message(f'已切换到{THEME_LABELS[theme]}，偏好已保存。', 'accent')

    def apply_port(self):
        if self.busy:
            return
        if self.last_status is None or self.last_status['running'] or self.last_status['managed']:
            self.message('请先停止当前服务，再应用新端口。', 'warn')
            return
        try:
            port = parse_port(self.port_entry.text())
        except ValueError as error:
            self.message(str(error), 'error')
            return
        self.queue_action('port', port)

    @property
    def busy(self):
        return self.pending is not None or self.operation not in (None, 'status')

    def show_log_menu(self, position):
        menu = self.logs.createStandardContextMenu()
        menu.addSeparator()
        clear_action = menu.addAction('清空日志')
        chosen = menu.exec_(self.logs.mapToGlobal(position))
        menu.deleteLater()
        if chosen == clear_action:
            self.clear_log()

    def clear_log(self):
        path = ROOT / '.runtime' / f'panel-{self.port}.log'
        try:
            path.write_text('', encoding='utf-8')
        except OSError as error:
            self.message(f'日志清空失败：{error}', 'error')
            return
        self.log_text = None
        self.refresh()
        self.message('日志已清空。', 'accent')

    def update_controls(self):
        status = self.last_status or {}
        running, managed = status.get('running', False), status.get('managed', False)
        ready = bool(status) and not self.busy
        set_enabled(self.start_button, ready and not running and not managed and not status.get('occupied', False))
        set_enabled(self.stop_button, ready and managed)
        set_enabled(self.restart_button, ready and managed)
        set_enabled(self.open_button, ready and running)
        set_enabled(self.refresh_button, not self.busy)
        for widget in (self.port_entry, self.apply_button):
            set_enabled(widget, ready and not running and not managed)

    def refresh(self):
        if self.future is None and not self.closed:
            self.begin('status')

    def begin(self, action, payload=None):
        self.operation = action
        self.future = self.pool.submit(self.work, action, self.port, payload)
        if action != 'status':
            self.update_controls()

    @staticmethod
    def inspect(port):
        status = service_status(port)
        path = ROOT / '.runtime' / f'panel-{port}.log'
        try:
            with path.open('rb') as file:
                file.seek(0, 2)
                file.seek(max(0, file.tell() - 24576))
                log = file.read().decode('utf-8', errors='replace')
        except FileNotFoundError:
            log = '暂无日志。启动服务后，运行信息将显示在这里。'
        return port, status, log

    @classmethod
    def work(cls, action, port, payload):
        if action == 'port':
            status = service_status(port)
            if status['running'] or status['managed']:
                raise ValueError('服务正在运行，请先停止，再应用新端口。')
            save_preferences({'port': payload})
            port = payload
        elif action == 'restart':
            restart(port)
        elif action in ('start', 'stop'):
            launch(port, stop=action == 'stop', browser=False)
        return cls.inspect(port)

    def queue_action(self, action, payload=None):
        if self.busy:
            return
        names = {'start': '正在启动…', 'stop': '正在停止…', 'restart': '正在重启…', 'port': '正在应用端口…'}
        self.message(names[action], 'accent')
        if self.future is not None:
            self.pending = (action, payload)
            self.update_controls()
        else:
            self.begin(action, payload)

    def execute(self, action):
        self.queue_action(action)

    def open_web(self):
        try:
            open_panel(self.port)
        except OSError as error:
            self.message(str(error), 'error')

    def render(self, status, log):
        self.last_status = status
        if status['running']:
            title = '● 运行中' if status['managed'] else '● 运行中（外部启动）'
            role = 'accent'
        elif status['managed']:
            title, role = '● 启动中或状态检查失败', 'warn'
        elif status['occupied']:
            title, role = '● 端口被其他服务占用', 'error'
        else:
            title, role = '○ 已停止', 'muted'
        set_text(self.status_label, title)
        if self.status_role != role:
            self.status_role = role
            self.tint(self.status_label, role)
        pid = status['pid'] if status['managed'] else '—'
        detail = f"PID {pid}    规则 {status['rules']}    监听 {status['listeners']}    活动连接 {status['connections']}" if status['rules'] is not None else f'PID {pid}    转发状态：—'
        set_text(self.detail_label, detail)
        self.update_controls()
        if status['error']:
            self.message(status['error'], 'error')
        elif status['running'] and not status['managed']:
            self.message('服务由其他方式启动，请在原终端停止；本控制台仍可打开 Web 面板。', 'warn')
        elif status['occupied'] and not status['managed']:
            self.message(f'端口 {self.port} 被其他服务占用，可选择新端口。', 'warn')
        self.update_log(log)

    def update_log(self, log):
        if log == self.log_text:
            return
        scroll = self.logs.verticalScrollBar()
        previous = scroll.value()
        at_bottom = previous >= scroll.maximum() - 2
        if self.log_text and log.startswith(self.log_text):
            cursor = QtGui.QTextCursor(self.logs.document())
            cursor.movePosition(QtGui.QTextCursor.End)
            cursor.insertText(log[len(self.log_text):])
        else:
            self.logs.setPlainText(log)
        scroll.setValue(scroll.maximum() if at_bottom else previous)
        self.log_text = log

    def tick(self):
        if self.closed or self.future is None or not self.future.done():
            return
        future, action = self.future, self.operation
        self.future = self.operation = None
        try:
            port, status, log = future.result()
        except Exception as error:
            self.message(str(error), 'error')
            self.update_controls()
        else:
            if port != self.port:
                self.port = port
                self.port_entry.setText(str(port))
                set_text(self.address_label, f'管理地址  http://127.0.0.1:{port}/')
                self.log_text = None
            if action != 'status':
                messages = {'start': '服务已启动。', 'stop': '服务及转发已停止。', 'restart': '服务已重启。', 'port': f'Web 端口已保存为 {port}，点击「启动」使用新端口。'}
                self.message(messages[action], 'accent')
            self.render(status, log)
        if self.pending is not None:
            action, payload = self.pending
            self.pending = None
            self.begin(action, payload)

    def closeEvent(self, event):
        self.closed = True
        self.tick_timer.stop()
        self.poll_timer.stop()
        self.pool.shutdown(wait=False, cancel_futures=True)
        event.accept()


def show_controller(port=8765):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app.setStyle('Fusion')
    controller = Controller(port)
    controller.show()
    return app.exec_()
