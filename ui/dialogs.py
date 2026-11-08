"""对话框：玩家席位编辑、游戏导入、规则设置。"""

from __future__ import annotations

import json
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
                               QSpinBox, QVBoxLayout, QWidget)

from core.players import SeatConfig

PROGRAM_FILTER = "程序 (*.py *.pyw *.exe *.bat *.cmd *.jar *.js);;所有文件 (*.*)"


class _PathRow(QWidget):
    """一行：输入框 + 浏览按钮。"""

    def __init__(self, text: str = "", filt: str = PROGRAM_FILTER, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.edit = QLineEdit(text)
        self.edit.setPlaceholderText("选择程序文件，或直接填命令")
        btn = QPushButton("浏览…")
        btn.setFixedWidth(64)
        btn.clicked.connect(lambda: self._browse(filt))
        lay.addWidget(self.edit, 1)
        lay.addWidget(btn)

    def _browse(self, filt: str) -> None:
        start = os.path.dirname(self.edit.text()) or os.getcwd()
        path, _ = QFileDialog.getOpenFileName(self, "选择程序", start, filt)
        if path:
            self.edit.setText(os.path.normpath(path))

    def text(self) -> str:
        return self.edit.text().strip()

    def setText(self, value: str) -> None:
        self.edit.setText(value)


class SeatDialog(QDialog):
    """编辑一个玩家席位。"""

    def __init__(self, seat: SeatConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("玩家席位")
        self.setMinimumWidth(520)
        self.seat = seat

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)

        self.name_edit = QLineEdit(seat.name)
        self.kind_box = QComboBox()
        self.kind_box.addItem("独立进程（任意语言，走 JSON 协议）", "process")
        self.kind_box.addItem("Python 模块（实现 decide 函数）", "module")
        self.kind_box.setCurrentIndex(0 if seat.kind == "process" else 1)

        self.path_row = _PathRow(seat.target)
        self.args_edit = QLineEdit(seat.args)
        self.args_edit.setPlaceholderText("可选，例如 --depth 6")
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(50, 600000)
        self.timeout_spin.setSingleStep(100)
        self.timeout_spin.setSuffix(" ms")
        self.timeout_spin.setValue(seat.timeout_ms)
        self.note_edit = QLineEdit(seat.note)

        form.addRow("名称", self.name_edit)
        form.addRow("接入方式", self.kind_box)
        form.addRow("程序", self.path_row)
        form.addRow("启动参数", self.args_edit)
        form.addRow("单回合超时", self.timeout_spin)
        form.addRow("备注", self.note_edit)

        tip = QLabel("进程式玩家：从 stdin 读 JSON、向 stdout 写 JSON；\n"
                     "模块式玩家：只要实现 decide(view, turn, ctx) 即可。")
        tip.setStyleSheet("color:#6b7280; font-size:12px;")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addWidget(tip)
        root.addStretch(1)
        root.addWidget(buttons)

    def _accept(self) -> None:
        if not self.name_edit.text().strip():
            QMessageBox.warning(self, "提示", "请填写名称")
            return
        if not self.path_row.text():
            QMessageBox.warning(self, "提示", "请选择程序文件")
            return
        if not os.path.exists(self.path_row.text()):
            QMessageBox.warning(self, "提示", "程序文件不存在")
            return
        self.accept()

    def result_seat(self) -> SeatConfig:
        s = self.seat
        s.name = self.name_edit.text().strip()
        s.kind = self.kind_box.currentData()
        s.target = self.path_row.text()
        s.args = self.args_edit.text().strip()
        s.timeout_ms = self.timeout_spin.value()
        s.note = self.note_edit.text().strip()
        return s


class GameDialog(QDialog):
    """导入 / 编辑上游游戏。"""

    def __init__(self, kind: str, target: str, args: str,
                 config: dict, recent: list | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("上游游戏")
        self.setMinimumWidth(560)
        self.recent = [g for g in (recent or []) if isinstance(g, dict) and g.get("target")]

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)

        if self.recent:
            self.recent_box = QComboBox()
            self.recent_box.addItem("（手动选择）", -1)
            for i, g in enumerate(self.recent):
                tag = "插件" if g.get("kind") == "plugin" else "进程"
                self.recent_box.addItem(f"{os.path.basename(g['target'])}（{tag}）", i)
            self.recent_box.currentIndexChanged.connect(self._pick_recent)
            form.addRow("最近使用", self.recent_box)

        self.kind_box = QComboBox()
        self.kind_box.addItem("独立进程（游戏自己跑回合循环）", "process")
        self.kind_box.addItem("Python 插件（中转站驱动 run(ctx)）", "plugin")
        self.kind_box.setCurrentIndex(0 if kind != "plugin" else 1)

        self.path_row = _PathRow(target)
        self.args_edit = QLineEdit(args)
        self.args_edit.setPlaceholderText("可选，进程式游戏的启动参数")
        self.config_edit = QPlainTextEdit(json.dumps(config or {}, ensure_ascii=False, indent=2))
        self.config_edit.setFixedHeight(110)

        form.addRow("接入方式", self.kind_box)
        form.addRow("游戏程序", self.path_row)
        form.addRow("启动参数", self.args_edit)
        form.addRow("游戏配置", self.config_edit)

        tip = QLabel("游戏配置是 JSON，会原样塞进 init 消息的 config 字段。\n"
                     "例如暗标竞拍：{\"rounds\": 8, \"budget\": 100}")
        tip.setStyleSheet("color:#6b7280; font-size:12px;")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addWidget(tip)
        root.addWidget(buttons)

    def _pick_recent(self, index: int) -> None:
        i = self.recent_box.itemData(index)
        if not isinstance(i, int) or i < 0 or i >= len(self.recent):
            return
        g = self.recent[i]
        self.kind_box.setCurrentIndex(1 if g.get("kind") == "plugin" else 0)
        self.path_row.setText(str(g.get("target", "")))
        self.args_edit.setText(str(g.get("args", "")))

    def _accept(self) -> None:
        if not self.path_row.text() or not os.path.exists(self.path_row.text()):
            QMessageBox.warning(self, "提示", "请选择存在的游戏程序")
            return
        try:
            json.loads(self.config_edit.toPlainText() or "{}")
        except json.JSONDecodeError as exc:
            QMessageBox.warning(self, "提示", f"游戏配置不是合法 JSON：{exc}")
            return
        self.accept()

    def result_spec(self) -> dict:
        try:
            cfg = json.loads(self.config_edit.toPlainText() or "{}")
        except json.JSONDecodeError:
            cfg = {}
        return {
            "kind": self.kind_box.currentData(),
            "target": self.path_row.text(),
            "args": self.args_edit.text().strip(),
            "config": cfg if isinstance(cfg, dict) else {},
        }


class SettingsDialog(QDialog):
    """全局规则设置。"""

    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("对局规则")
        self.setMinimumWidth(460)
        self.settings = dict(settings)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)

        self.seed_spin = QSpinBox()
        self.seed_spin.setRange(0, 2 ** 31 - 1)
        self.seed_spin.setValue(int(settings.get("seed", 1)))

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(50, 600000)
        self.timeout_spin.setSingleStep(100)
        self.timeout_spin.setSuffix(" ms")
        self.timeout_spin.setValue(int(settings.get("default_timeout_ms", 2000)))

        self.violation_spin = QSpinBox()
        self.violation_spin.setRange(0, 99)
        self.violation_spin.setValue(int(settings.get("max_violations", 3)))
        self.violation_spin.setSpecialValueText("一次即判负")

        self.persistent_box = QCheckBox("玩家进程常驻（跨回合保留内部状态）")
        self.persistent_box.setChecked(bool(settings.get("persistent_players", True)))
        self.retry_box = QCheckBox("协议类错误自动重试一次")
        self.retry_box.setChecked(bool(settings.get("retry_invalid", False)))

        form.addRow("随机种子", self.seed_spin)
        form.addRow("默认超时", self.timeout_spin)
        form.addRow("违规上限", self.violation_spin)
        form.addRow("", self.persistent_box)
        form.addRow("", self.retry_box)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addWidget(buttons)

    def result_settings(self) -> dict:
        s = dict(self.settings)
        s["seed"] = self.seed_spin.value()
        s["default_timeout_ms"] = self.timeout_spin.value()
        s["max_violations"] = self.violation_spin.value()
        s["persistent_players"] = self.persistent_box.isChecked()
        s["retry_invalid"] = self.retry_box.isChecked()
        return s
