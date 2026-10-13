"""书架上的 AI 设置。修改后下一次阅读使用新配置。"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from PySide6.QtGui import QGuiApplication

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSpinBox,
                               QScrollArea, QVBoxLayout, QWidget)
from reader.ai import api, claude, config, harness, provider, secrets
from reader.ui import theme
from reader.ui.shelf_kit import background

_PROTOCOL_LABELS = {'claude_code': 'Claude Code（本机登录）', 'responses': 'OpenAI Responses',
                    'chat_completions': 'OpenAI 兼容 Chat Completions',
                    'anthropic_messages': 'Claude Messages API', 'local_harness': '本地 Harness 桥接'}


class AISettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('AI 设置')
        self.resize(580, 310)
        self.setMinimumWidth(570)
        self.setFont(theme.font(13, 400))
        self._keys = {}
        self._current = None
        self._busy = False
        self._advanced = False
        try:
            self.data = config.load()
            initial_error = ''
        except config.ConfigError as e:
            self.data = config.defaults()
            initial_error = str(e) + '。保存后会用这里的设置替换原配置。'
        self.profiles = config.validate(self.data)
        self.profiles.setdefault('harness', config.validate(config.defaults())['harness'])
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 22)
        self.status = QLabel(initial_error or '选一个 AI，填上 API Key，点连接就能用。')
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        form = QFormLayout()
        self.choice = QComboBox()
        for p in self.profiles.values():
            self.choice.addItem(p.label, p.id)
        pick = QHBoxLayout()
        pick.addWidget(self.choice, 1)
        self.add_btn = QPushButton('新增配置')
        self.add_btn.clicked.connect(self._add)
        pick.addWidget(self.add_btn)
        form.addRow('使用的 AI', pick)
        self.fields = {}
        for field, label in [('label', '显示名称'), ('model', '模型 ID'), ('base_url', '接口根地址'),
                             ('api_key_env', 'Key 环境变量'), ('auth_header', '认证头名称'),
                             ('claude_config_dir', 'Claude 登录目录'), ('cli_path', 'Claude 程序路径')]:
            edit = QLineEdit()
            self.fields[field] = edit
            form.addRow(label, edit)
        self.fields['base_url'].setPlaceholderText('例如 https://api.openai.com/v1，不含 /responses')
        self.fields['cli_path'].setPlaceholderText('留空会自动寻找本机 Claude Code')
        self.protocol = QComboBox()
        for value, label in _PROTOCOL_LABELS.items():
            self.protocol.addItem(label, value)
        form.insertRow(2, '接口类型', self.protocol)
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.Password)
        self.key.setPlaceholderText('粘贴你的 API Key；已填过可以留空')
        form.addRow('API Key', self.key)
        self.clear_key = QCheckBox('删除这个配置已保存的 Key')
        form.addRow('', self.clear_key)
        self.images = QCheckBox('此模型支持图片（PDF 图表问答）')
        form.addRow('', self.images)
        self.timeout = QSpinBox()
        self.timeout.setRange(1, 1800)
        self.timeout.setSuffix(' 秒')
        form.addRow('等待上限', self.timeout)
        self.max_tokens = QSpinBox()
        self.max_tokens.setRange(16, 131072)
        form.addRow('输出 Token 上限', self.max_tokens)
        self.token_parameter = QComboBox()
        self.token_parameter.addItems(['max_tokens', 'max_completion_tokens'])
        form.addRow('兼容接口输出参数', self.token_parameter)
        self.extra = QPlainTextEdit()
        self.extra.setMaximumHeight(80)
        form.addRow('附加参数（JSON）', self.extra)
        self.harness_command = QPlainTextEdit()
        self.harness_command.setMaximumHeight(70)
        form.addRow('Harness 命令（JSON）', self.harness_command)
        self.harness_help = QLabel('点下面的按钮复制提示词，粘贴给你常用的本地 AI 工具。\n它会读取这个项目并完成接入。')
        self.harness_help.setWordWrap(True)
        form.addRow('', self.harness_help)
        self.code_help = QLabel('使用本机 Claude Code 的独立读书账号。点连接会自动检查，未登录时会打开登录窗口。')
        self.code_help.setWordWrap(True)
        form.addRow('', self.code_help)
        self.advanced_btn = QPushButton('高级选项')
        self.advanced_btn.clicked.connect(self._toggle_advanced)
        form.addRow('', self.advanced_btn)
        form_host = QWidget()
        form_host.setLayout(form)
        self.form = form
        self.form_host = form_host
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(form_host)
        outer.addWidget(scroll, 1)
        self.hint = QLabel('Key 加密保存在你的电脑上。连接会发送一条简短测试请求。')
        self.hint.setWordWrap(True)
        outer.addWidget(self.hint)
        self.override = QLabel()
        self.override.setWordWrap(True)
        overrides = [k for k in ('READER_AI', 'READER_MODEL', 'READER_BASE_URL',
                                'READER_CLAUDE_CONFIG_DIR', 'CLAUDE_CLI') if os.environ.get(k)]
        self.override.setText('环境变量正在覆盖设置：' + '、'.join(overrides) if overrides else '')
        outer.addWidget(self.override)
        actions = QHBoxLayout()
        self.check_btn = QPushButton('检查凭据')
        self.check_btn.clicked.connect(lambda: self._check(False))
        self.test_btn = QPushButton('保存并测试连接')
        self.test_btn.clicked.connect(lambda: self._check(True))
        self.save_btn = QPushButton('连接并使用')
        self.save_btn.clicked.connect(self._quick_connect)
        cancel = QPushButton('取消')
        cancel.clicked.connect(self.reject)
        for b in (self.check_btn, self.test_btn, self.save_btn, cancel):
            actions.addWidget(b)
        outer.addLayout(actions)
        selected = self.data['active']
        if selected == 'auto':
            try:
                selected = provider.name()
            except config.ConfigError:
                selected = 'claude'
        self.choice.setCurrentIndex(max(0, self.choice.findData(selected)))
        self._load_choice()
        self.choice.currentIndexChanged.connect(self._switch)
        self.protocol.currentIndexChanged.connect(self._protocol_visibility)
        theme.style(self, 'QDialog {{ background: {bg2}; color: {t1}; }} '
                          'QScrollArea, QScrollArea > QWidget > QWidget {{ background: {bg2}; border: none; }} '
                          'QLabel, QCheckBox {{ color: {t1}; }} '
                          'QLineEdit, QComboBox, QSpinBox, QPlainTextEdit {{ background: {bg3}; color: {t1}; '
                          'border: 1px solid {l2}; border-radius: 5px; padding: 5px; }} '
                          'QPushButton {{ background: {bg3}; color: {t1}; border: 1px solid {l2}; '
                          'border-radius: 6px; padding: 7px; }}')

    def _stash(self):
        if self._current is None:
            return
        raw = asdict(self.profiles[self._current])
        raw.update({k: v.text().strip() for k, v in self.fields.items()})
        raw.update(protocol=self.protocol.currentData(), supports_images=self.images.isChecked(),
                   timeout=self.timeout.value(), max_tokens=self.max_tokens.value(),
                   token_parameter=self.token_parameter.currentText())
        try:
            raw['extra_body'] = json.loads(self.extra.toPlainText() or '{}')
            raw['harness_command'] = json.loads(self.harness_command.toPlainText() or '[]')
        except ValueError as e:
            raise config.ConfigError('附加参数不是有效的 JSON') from e
        p = config.Profile(**raw)
        p.validate()
        self.profiles[self._current] = p
        if self.key.text().strip():
            self._keys[self._current] = self.key.text().strip()
        elif self.clear_key.isChecked():
            self._keys[self._current] = ''

    def _switch(self):
        try:
            self._stash()
        except config.ConfigError as e:
            self.status.setText(str(e))
            self.choice.blockSignals(True)
            self.choice.setCurrentIndex(self.choice.findData(self._current))
            self.choice.blockSignals(False)
            return
        self._load_choice()

    def _load_choice(self):
        self._current = self.choice.currentData()
        p = self.profiles[self._current]
        for k, edit in self.fields.items():
            edit.setText(getattr(p, k))
        self.protocol.setCurrentIndex(self.protocol.findData(p.protocol))
        self.images.setChecked(p.supports_images)
        self.timeout.setValue(p.timeout)
        self.max_tokens.setValue(p.max_tokens)
        self.token_parameter.setCurrentText(p.token_parameter)
        self.extra.setPlainText(json.dumps(p.extra_body, ensure_ascii=False))
        self.harness_command.setPlainText(json.dumps(p.harness_command, ensure_ascii=False))
        self.key.clear()
        # Keep a pending deletion visible when the user switches away and back.
        self.clear_key.setChecked(self._current in self._keys and self._keys[self._current] == '')
        self._protocol_visibility()

    def _protocol_visibility(self):
        code = self.protocol.currentData() == 'claude_code'
        local = self.protocol.currentData() == 'local_harness'
        http = not code and not local
        for k in ('base_url', 'api_key_env', 'auth_header'):
            self.fields[k].setEnabled(http)
        for k in ('claude_config_dir', 'cli_path'):
            self.fields[k].setEnabled(code)
        for w in (self.key, self.clear_key, self.extra, self.max_tokens):
            w.setEnabled(http)
        self.token_parameter.setEnabled(self.protocol.currentData() == 'chat_completions')
        if hasattr(self, 'form'):
            for k in ('label', 'api_key_env', 'auth_header', 'cli_path'):
                relevant = code if k == 'cli_path' else http if k in ('api_key_env', 'auth_header') else True
                self.form.setRowVisible(self.fields[k], self._advanced and relevant)
            self.form.setRowVisible(self.protocol, self._advanced)
            self.form.setRowVisible(self.fields['model'], self._advanced)
            self.form.setRowVisible(self.fields['base_url'], self._advanced and http)
            self.form.setRowVisible(self.fields['claude_config_dir'], self._advanced and code)
            self.form.setRowVisible(self.key, http)
            self.form.setRowVisible(self.clear_key, self._advanced and http)
            self.form.setRowVisible(self.images, self._advanced and not code)
            self.form.setRowVisible(self.harness_command, self._advanced and local)
            self.form.setRowVisible(self.harness_help, local)
            self.form.setRowVisible(self.code_help, code)
            for w in (self.timeout, self.max_tokens, self.token_parameter, self.extra):
                relevant = http if w in (self.max_tokens, self.extra) else self.protocol.currentData() == 'chat_completions' if w is self.token_parameter else True
                self.form.setRowVisible(w, self._advanced and relevant)
            self.add_btn.setVisible(self._advanced)
            self.check_btn.setVisible(self._advanced)
            self.test_btn.setVisible(self._advanced)
            self.override.setVisible(self._advanced)
            p = self.profiles.get(self._current)
            self.save_btn.setText('复制接入提示词' if local and p and not harness.ready(p) else '连接并使用')
            self.hint.setText('把提示词给本地 Harness，它会自动配置这个项目。' if local else
                              '直接使用本机 Claude Code。' if code else
                              'Key 加密保存在你的电脑上。连接会发送一条简短测试请求。')

    def _toggle_advanced(self):
        self._advanced = not self._advanced
        self.advanced_btn.setText('收起高级选项' if self._advanced else '高级选项')
        self.resize(640, 760) if self._advanced else self.resize(580, 310)
        self._protocol_visibility()

    def _quick_connect(self):
        try:
            self._stash()
        except config.ConfigError as e:
            self.status.setText(str(e))
            return
        p = self.profiles[self._current]
        if p.protocol == 'local_harness' and not harness.ready(p):
            QGuiApplication.clipboard().setText(harness.setup_prompt())
            self.status.setText('提示词已复制。粘贴给本地 Harness，让它直接完成接入；完成后重新打开这里。')
            return
        self._check(True, close_on_success=True)

    def _add(self):
        try:
            self._stash()
        except config.ConfigError as e:
            self.status.setText(str(e))
            return
        i = 1
        while f'custom_{i}' in self.profiles:
            i += 1
        p = config.Profile(f'custom_{i}', f'自定义 {i}', 'chat_completions', 'your-model-id',
                           'http://localhost:11434/v1', supports_images=False)
        self.profiles[p.id] = p
        self.choice.addItem(p.label, p.id)
        self.choice.setCurrentIndex(self.choice.count() - 1)

    def _save(self):
        self._stash()
        data = {'version': 1, 'active': self._current, 'profiles': [asdict(p) for p in self.profiles.values()]}
        config.validate(data)
        for pid, key in self._keys.items():
            secrets.put(pid, key)
        config.save(data)
        self._keys.clear()
        self.key.clear()
        self.clear_key.setChecked(False)

    def _check(self, live, close_on_success=False):
        if self._busy:
            return
        try:
            if live:
                self._save()
                p = provider.profile()
            else:
                self._stash()
                p = self.profiles[self._current]
        except (config.ConfigError, OSError) as e:
            self.status.setText(str(e))
            return
        self._busy = True
        self.form_host.setEnabled(False)
        for w in (self.choice, self.check_btn, self.test_btn, self.save_btn):
            w.setEnabled(False)
        self.status.setText('正在测试连接…' if live else '正在检查本机凭据…')

        def check():
            if live:
                if p.protocol == 'claude_code' and not claude.auth_status().get('loggedIn'):
                    return (False, 'need_login')
                r = provider.ask_once('只回答 OK。', system='这是连接测试。', timeout=min(p.timeout, 60))
                return (r.ok, '连接成功，已收到模型回答。' if r.ok else r.error)
            if p.protocol == 'local_harness':
                return '已找到本地连接；请测试连接确认可用。' if harness.ready(p) else '先复制接入提示词给本地 Harness。'
            if p.protocol == 'claude_code':
                # 未保存的登录目录也能检查；不弹登录窗口。
                import subprocess
                exe = p.cli_path or claude.cli_path()
                if not exe:
                    return '未找到 Claude Code，请填写程序路径或安装后重试。'
                env = dict(os.environ, CLAUDE_CONFIG_DIR=str(Path(p.claude_config_dir).expanduser()) if p.claude_config_dir else str(claude.CONFIG_DIR))
                r = subprocess.run([exe, 'auth', 'status'], capture_output=True, text=True,
                                   encoding='utf-8', env=env, timeout=30,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                return 'Claude Code 已登录；尚未发送模型请求。' if json.loads(r.stdout or '{}').get('loggedIn') else 'Claude Code 尚未登录。'
            present = bool(self._keys.get(p.id)) if p.id in self._keys else api.ready(p)
            return '已找到本机凭据；请测试连接确认模型可用。' if present else '尚未填写 API Key。'

        def done(value, error):
            self._busy = False
            self.form_host.setEnabled(True)
            for w in (self.choice, self.check_btn, self.test_btn, self.save_btn):
                w.setEnabled(True)
            if error:
                self.status.setText('检查失败，请查看设置和网络。')
            elif live:
                ok, text = value
                if text == 'need_login':
                    from reader.ui.login import open_login_window
                    try:
                        open_login_window()
                        self.status.setText('已打开登录窗口。登录后回来点“连接并使用”即可。')
                    except OSError:
                        self.status.setText('登录窗口未能打开，请从书架的登录提示完成登录。')
                else:
                    self.status.setText(str(text))
                    if ok and close_on_success:
                        self.accept()
            else:
                self.status.setText(str(value))
        background(check, done)
