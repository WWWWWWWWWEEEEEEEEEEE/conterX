"""主窗口：对局台 / 批量对抗 / 协议文档。"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime

from PySide6.QtCore import QThread, Qt, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFileDialog,
                               QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMainWindow,
                               QMessageBox, QPlainTextEdit, QProgressBar,
                               QPushButton, QSpinBox, QSplitter, QTableWidget,
                               QTableWidgetItem, QTabWidget, QTextBrowser,
                               QVBoxLayout, QWidget)

from core.batch import BatchConfig, BatchRunner
from core.engine import MatchConfig, MatchEngine
from core.games import make_game
from core.players import SeatConfig
from core.store import Store

from .dialogs import GameDialog, SeatDialog, SettingsDialog

KIND_LABEL = {
    "system": "系统", "turn": "回合", "action": "动作", "violation": "违规",
    "player": "玩家", "raw": "原始", "error": "错误", "result": "结果", "game": "游戏",
}
KIND_COLOR = {
    "turn": "#2563eb", "action": "#374151", "violation": "#dc2626",
    "error": "#dc2626", "result": "#059669", "game": "#7c3aed",
    "player": "#0891b2", "raw": "#9ca3af", "system": "#6b7280",
}
# 批量运行时只把这些类型送进界面：回合/动作每局上千条，全推过去会把事件循环淹了
BATCH_LOG_KINDS = ("violation", "error", "result", "system")


class MatchThread(QThread):
    sig_log = Signal(dict)
    sig_finish = Signal(object)

    def __init__(self, engine: MatchEngine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.report = None
        self.err = ""

    def run(self) -> None:
        try:
            self.report = self.engine.run()
        except Exception as exc:  # noqa: BLE001
            self.err = str(exc)
        finally:
            self.sig_finish.emit(self.report)


class BatchThread(QThread):
    sig_progress = Signal(int, int, object)
    sig_log = Signal(dict)
    sig_finish = Signal(object)

    def __init__(self, runner: BatchRunner, parent=None):
        super().__init__(parent)
        self.runner = runner
        self.report = None
        self.err = ""

    def run(self) -> None:
        try:
            self.report = self.runner.run()
        except Exception as exc:  # noqa: BLE001
            self.err = str(exc)
        finally:
            self.sig_finish.emit(self.report)


class MainWindow(QMainWindow):
    def __init__(self, store: Store):
        super().__init__()
        self.store = store
        self.settings = store.load_settings()
        self.seats = store.load_players()
        self.thread: MatchThread | None = None
        self.batch_thread: BatchThread | None = None
        self._refreshing = False
        self._game_spec = self._load_game_spec()

        self.setWindowTitle("conterX · 算法对抗中转站")
        self.resize(1360, 820)
        self._build_ui()
        self._refresh_seats()
        self._refresh_game_bar()

    # ------------------------------------------------------------------
    # 游戏配置
    # ------------------------------------------------------------------
    def _load_game_spec(self) -> dict:
        g = self.settings.get("game") or {}
        spec = {
            "kind": g.get("kind", "process"),
            "target": g.get("target", ""),
            "args": g.get("args", ""),
            "config": g.get("config") or {},
        }
        if not spec["target"]:
            guess = os.path.join(self.store.base_dir, "examples", "games", "sealed_bid.py")
            if os.path.exists(guess):
                spec["target"] = guess
                spec["kind"] = "process"
        return spec

    def _save_game_spec(self) -> None:
        self.settings["game"] = dict(self._game_spec)
        self.store.save_settings(self.settings)

    def _refresh_game_bar(self) -> None:
        target = self._game_spec.get("target", "")
        self.game_path_edit.setText(target or "（未选择游戏）")
        self.game_path_edit.setToolTip(target)
        kind = "插件" if self._game_spec.get("kind") == "plugin" else "进程"
        cfg = self._game_spec.get("config") or {}
        brief = json.dumps(cfg, ensure_ascii=False)
        if len(brief) > 24:
            brief = brief[:24] + "…"
        self.game_meta_label.setText(f"〔{kind}〕" + (f" {brief}" if cfg else ""))
        self.game_meta_label.setToolTip(json.dumps(cfg, ensure_ascii=False, indent=2) if cfg else "无额外配置")
        self.game_path_edit.setToolTip(target)

    # ------------------------------------------------------------------
    # 界面搭建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        tabs = QTabWidget()
        tabs.addTab(self._build_match_tab(), "对局台")
        tabs.addTab(self._build_batch_tab(), "批量对抗")
        tabs.addTab(self._build_doc_tab(), "协议与模板")
        self.setCentralWidget(tabs)
        self.tabs = tabs

        bar = self.statusBar()
        self.status_label = QLabel("就绪")
        bar.addWidget(self.status_label)
        bar.addPermanentWidget(QLabel("conterX v0.1 · 协议 conterx/1"))

        self._build_menu()

    def _build_menu(self) -> None:
        m = self.menuBar().addMenu("文件")
        act = m.addAction("导入游戏…")
        act.triggered.connect(self._edit_game)
        act = m.addAction("添加玩家…")
        act.triggered.connect(self._add_seat)
        m.addSeparator()
        act = m.addAction("打开数据目录")
        act.triggered.connect(lambda: self._open_path(self.store.data_dir))
        m.addSeparator()
        act = m.addAction("退出")
        act.triggered.connect(self.close)

        m2 = self.menuBar().addMenu("帮助")
        act = m2.addAction("关于")
        act.triggered.connect(self._about)

    # -- 对局台 ---------------------------------------------------------
    def _build_match_tab(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(10, 10, 10, 6)
        root.setSpacing(8)

        # 游戏条
        game_box = QGroupBox("上游游戏")
        g = QVBoxLayout(game_box)
        g.setSpacing(6)
        row1 = QHBoxLayout()
        self.game_path_edit = QLineEdit()
        self.game_path_edit.setReadOnly(True)
        self.game_path_edit.setPlaceholderText("选择一个游戏程序")
        btn_choose = QPushButton("选择游戏…")
        btn_choose.clicked.connect(self._edit_game)
        self.game_meta_label = QLabel("")
        self.game_meta_label.setStyleSheet("color:#6b7280;")
        row1.addWidget(QLabel("程序"))
        row1.addWidget(self.game_path_edit, 1)
        row1.addWidget(btn_choose)
        row1.addWidget(self.game_meta_label)
        g.addLayout(row1)

        row2 = QHBoxLayout()
        self.btn_start = QPushButton("开始对局")
        self.btn_start.setObjectName("primary")
        self.btn_start.clicked.connect(lambda: self._start_match(False))
        self.btn_pause = QPushButton("暂停")
        self.btn_pause.clicked.connect(self._toggle_pause)
        self.btn_step = QPushButton("单步")
        self.btn_step.clicked.connect(self._step)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.clicked.connect(self._stop_match)
        self.btn_rule = QPushButton("规则…")
        self.btn_rule.clicked.connect(self._edit_settings)

        self.seed_spin = QSpinBox()
        self.seed_spin.setRange(0, 2 ** 31 - 1)
        self.seed_spin.setValue(int(self.settings.get("seed", 1)))
        self.seed_spin.setPrefix("种子 ")
        self.seed_spin.setFixedWidth(126)
        self.seed_spin.valueChanged.connect(self._sync_settings_from_bar)

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(50, 600000)
        self.timeout_spin.setSingleStep(100)
        self.timeout_spin.setValue(int(self.settings.get("default_timeout_ms", 2000)))
        self.timeout_spin.setPrefix("超时 ")
        self.timeout_spin.setSuffix(" ms")
        self.timeout_spin.setFixedWidth(176)
        self.timeout_spin.valueChanged.connect(self._sync_settings_from_bar)

        self.violation_spin = QSpinBox()
        self.violation_spin.setRange(0, 99)
        self.violation_spin.setValue(int(self.settings.get("max_violations", 3)))
        self.violation_spin.setPrefix("违规上限 ")
        self.violation_spin.setFixedWidth(150)
        self.violation_spin.valueChanged.connect(self._sync_settings_from_bar)

        self.persistent_box = QCheckBox("常驻")
        self.persistent_box.setChecked(bool(self.settings.get("persistent_players", True)))
        self.persistent_box.setToolTip("玩家进程跨回合保留内部状态；关闭则每回合重启")
        self.persistent_box.toggled.connect(self._sync_settings_from_bar)

        self.retry_box = QCheckBox("协议错误重试")
        self.retry_box.setChecked(bool(self.settings.get("retry_invalid", False)))
        self.retry_box.toggled.connect(self._sync_settings_from_bar)

        for w in (self.btn_start, self.btn_pause, self.btn_step, self.btn_stop):
            row2.addWidget(w)
        row2.addSpacing(12)
        row2.addWidget(self.seed_spin)
        row2.addWidget(self.timeout_spin)
        row2.addWidget(self.violation_spin)
        row2.addWidget(self.persistent_box)
        row2.addWidget(self.retry_box)
        row2.addStretch(1)
        row2.addWidget(self.btn_rule)
        g.addLayout(row2)
        root.addWidget(game_box)

        # 三栏
        split = QSplitter(Qt.Orientation.Horizontal)

        left = QGroupBox("玩家席位")
        lv = QVBoxLayout(left)
        lv.setSpacing(6)
        self.seat_table = QTableWidget(0, 5)
        self.seat_table.setHorizontalHeaderLabels(["启用", "玩家", "接入", "程序", "状态"])
        self.seat_table.verticalHeader().setVisible(False)
        self.seat_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.seat_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.seat_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        hh = self.seat_table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.seat_table.setColumnWidth(0, 42)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.seat_table.setColumnWidth(1, 86)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.seat_table.setColumnWidth(2, 46)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.seat_table.setColumnWidth(4, 58)
        self.seat_table.itemChanged.connect(self._on_seat_item_changed)
        self.seat_table.doubleClicked.connect(lambda _: self._edit_seat())
        lv.addWidget(self.seat_table, 1)

        btns = QGridLayout()
        btns.setSpacing(4)
        for i, (text, slot) in enumerate([
            ("添加", self._add_seat), ("编辑", self._edit_seat), ("移除", self._remove_seat),
            ("上移", lambda: self._move_seat(-1)), ("下移", lambda: self._move_seat(1)),
        ]):
            b = QPushButton(text)
            b.clicked.connect(slot)
            btns.addWidget(b, i // 3, i % 3)
        lv.addLayout(btns)
        split.addWidget(left)

        center = QGroupBox("回合日志")
        cv = QVBoxLayout(center)
        cv.setSpacing(6)
        self.log_table = QTableWidget(0, 6)
        self.log_table.setHorizontalHeaderLabels(["#", "时间", "回合", "玩家", "类型", "内容"])
        self.log_table.verticalHeader().setVisible(False)
        self.log_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.log_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.log_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.log_table.setWordWrap(False)
        lh = self.log_table.horizontalHeader()
        lh.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.log_table.setColumnWidth(0, 48)
        lh.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.log_table.setColumnWidth(1, 84)
        lh.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.log_table.setColumnWidth(2, 48)
        lh.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.log_table.setColumnWidth(3, 72)
        lh.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.log_table.setColumnWidth(4, 56)
        lh.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.log_table.itemSelectionChanged.connect(self._on_log_selected)
        cv.addWidget(self.log_table, 1)
        split.addWidget(center)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(8)

        detail_box = QGroupBox("详情")
        dv = QVBoxLayout(detail_box)
        self.detail_view = QPlainTextEdit()
        self.detail_view.setReadOnly(True)
        self.detail_view.setPlaceholderText("选中中间日志行，这里显示原始请求 / 响应 JSON")
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(9)
        self.detail_view.setFont(font)
        dv.addWidget(self.detail_view)
        rv.addWidget(detail_box, 3)

        result_box = QGroupBox("结算")
        resv = QVBoxLayout(result_box)
        self.result_table = QTableWidget(0, 5)
        self.result_table.setHorizontalHeaderLabels(["名次", "玩家", "得分", "违规", "均耗时"])
        self.result_table.verticalHeader().setVisible(False)
        self.result_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.result_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        rh = self.result_table.horizontalHeader()
        rh.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        resv.addWidget(self.result_table, 1)
        self.result_tip = QLabel("尚未开始")
        self.result_tip.setStyleSheet("color:#6b7280;")
        self.result_tip.setWordWrap(True)
        resv.addWidget(self.result_tip)
        rv.addWidget(result_box, 2)
        split.addWidget(right)

        split.setSizes([330, 700, 372])
        root.addWidget(split, 1)
        return page

    # -- 批量页 ---------------------------------------------------------
    def _build_batch_tab(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(10, 10, 10, 6)
        root.setSpacing(8)

        box = QGroupBox("批量对抗（使用「对局台」当前选择的游戏与玩家席位）")
        v = QVBoxLayout(box)
        row = QHBoxLayout()
        self.batch_rounds = QSpinBox()
        self.batch_rounds.setRange(1, 10000)
        self.batch_rounds.setValue(int(self.settings.get("batch_rounds", 20)))
        self.batch_rounds.setPrefix("局数 ")
        self.batch_rounds.setFixedWidth(148)
        self.batch_seed = QSpinBox()
        self.batch_seed.setRange(0, 2 ** 31 - 1)
        self.batch_seed.setValue(int(self.settings.get("seed", 1)))
        self.batch_seed.setPrefix("起始种子 ")
        self.batch_seed.setFixedWidth(196)
        self.btn_batch_start = QPushButton("开始批量")
        self.btn_batch_start.setObjectName("primary")
        self.btn_batch_start.clicked.connect(self._start_batch)
        self.btn_batch_stop = QPushButton("停止")
        self.btn_batch_stop.clicked.connect(self._stop_batch)
        self.btn_batch_stop.setEnabled(False)
        self.btn_batch_csv = QPushButton("导出 CSV")
        self.btn_batch_csv.clicked.connect(self._export_batch_csv)
        self.btn_batch_csv.setEnabled(False)
        for w in (self.batch_rounds, self.batch_seed, self.btn_batch_start,
                  self.btn_batch_stop, self.btn_batch_csv):
            row.addWidget(w)
        row.addStretch(1)
        v.addLayout(row)

        self.batch_bar = QProgressBar()
        self.batch_bar.setRange(0, 100)
        self.batch_bar.setValue(0)
        self.batch_bar.setTextVisible(True)
        v.addWidget(self.batch_bar)
        self.batch_tip = QLabel("排行榜按胜场、平均得分、平均名次排序。")
        self.batch_tip.setStyleSheet("color:#6b7280;")
        v.addWidget(self.batch_tip)
        root.addWidget(box)

        self.batch_table = QTableWidget(0, 11)
        self.batch_table.setHorizontalHeaderLabels(
            ["名次", "玩家", "局数", "胜场", "胜率", "前二率", "平均名次",
             "平均得分", "违规", "判负", "均耗时"])
        self.batch_table.verticalHeader().setVisible(False)
        self.batch_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.batch_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.batch_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.batch_table, 1)

        log_box = QGroupBox("运行日志（回合摘要 / 违规 / 错误）")
        lb = QVBoxLayout(log_box)
        self.batch_log = QPlainTextEdit()
        self.batch_log.setReadOnly(True)
        self.batch_log.setMaximumBlockCount(300)
        self.batch_log.setFixedHeight(104)
        self.batch_log.setPlaceholderText(
            "批量运行时的关键事件会显示在这里；完整回合日志见 data/logs/")
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPointSize(9)
        self.batch_log.setFont(mono)
        lb.addWidget(self.batch_log)
        root.addWidget(log_box)

        self._batch_csv_path = ""
        return page

    # -- 文档页 ---------------------------------------------------------
    def _build_doc_tab(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(10, 10, 10, 6)
        root.setSpacing(8)

        row = QHBoxLayout()
        b1 = QPushButton("打开协议文档")
        b1.clicked.connect(lambda: self._open_path(os.path.join(self.store.base_dir, "PROTOCOL.md")))
        b2 = QPushButton("打开模板目录")
        b2.clicked.connect(lambda: self._open_path(os.path.join(self.store.base_dir, "sdk")))
        b3 = QPushButton("打开示例目录")
        b3.clicked.connect(lambda: self._open_path(os.path.join(self.store.base_dir, "examples")))
        b4 = QPushButton("打开日志目录")
        b4.clicked.connect(lambda: self._open_path(self.store.log_dir))
        b5 = QPushButton("重新载入")
        b5.clicked.connect(self._load_docs)
        for b in (b1, b2, b3, b4, b5):
            row.addWidget(b)
        row.addStretch(1)
        root.addLayout(row)

        self.doc_view = QTextBrowser()
        self.doc_view.setOpenExternalLinks(True)
        root.addWidget(self.doc_view, 1)
        self._load_docs()
        return page

    def _load_docs(self) -> None:
        path = os.path.join(self.store.base_dir, "PROTOCOL.md")
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
            self.doc_view.setMarkdown(text)
        except Exception:
            self.doc_view.setPlainText("未找到 PROTOCOL.md，请确认它和 main.py 在同一个目录。")

    # ------------------------------------------------------------------
    # 席位管理
    # ------------------------------------------------------------------
    def _refresh_seats(self) -> None:
        self._refreshing = True
        t = self.seat_table
        t.setRowCount(0)
        for seat in self.seats:
            row = t.rowCount()
            t.insertRow(row)
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Checked if seat.enabled else Qt.CheckState.Unchecked)
            chk.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            t.setItem(row, 0, chk)

            name = QTableWidgetItem(seat.name)
            name.setForeground(QBrush(QColor(seat.color)))
            f = name.font()
            f.setBold(True)
            name.setFont(f)
            name.setData(Qt.ItemDataRole.UserRole, seat.pid)
            t.setItem(row, 1, name)

            kind = QTableWidgetItem("进程" if seat.kind == "process" else "模块")
            kind.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            t.setItem(row, 2, kind)

            prog = QTableWidgetItem(os.path.basename(seat.target) if seat.target else "未设置")
            prog.setToolTip(f"{seat.target}\n超时 {seat.timeout_ms} ms　{seat.note}")
            t.setItem(row, 3, prog)

            st = QTableWidgetItem("待命")
            st.setToolTip(seat.note or "未开始")
            t.setItem(row, 4, st)
        self._refreshing = False

    def _on_seat_item_changed(self, item: QTableWidgetItem) -> None:
        if self._refreshing:
            return
        if item.column() != 0:
            return
        row = item.row()
        if 0 <= row < len(self.seats):
            self.seats[row].enabled = item.checkState() == Qt.CheckState.Checked
            self.store.save_players(self.seats)

    def _current_seat_index(self) -> int:
        rows = self.seat_table.selectionModel().selectedRows() if self.seat_table.selectionModel() else []
        if rows:
            return rows[0].row()
        return -1

    def _add_seat(self) -> None:
        pid = f"p{len(self.seats) + 1}"
        from core.players import SEAT_COLORS
        seat = SeatConfig(pid=pid, name=f"玩家{len(self.seats) + 1}",
                          kind="process", target="",
                          color=SEAT_COLORS[len(self.seats) % len(SEAT_COLORS)])
        dlg = SeatDialog(seat, self)
        if dlg.exec():
            self.seats.append(dlg.result_seat())
            self._refresh_seats()
            self.store.save_players(self.seats)

    def _edit_seat(self) -> None:
        idx = self._current_seat_index()
        if idx < 0 or idx >= len(self.seats):
            QMessageBox.information(self, "提示", "先在左侧选中一个玩家")
            return
        dlg = SeatDialog(self.seats[idx], self)
        if dlg.exec():
            self.seats[idx] = dlg.result_seat()
            self._refresh_seats()
            self.store.save_players(self.seats)

    def _remove_seat(self) -> None:
        idx = self._current_seat_index()
        if idx < 0 or idx >= len(self.seats):
            return
        if len(self.seats) <= 1:
            QMessageBox.information(self, "提示", "至少保留一个玩家")
            return
        if QMessageBox.question(self, "确认", f"移除「{self.seats[idx].name}」？") == QMessageBox.StandardButton.Yes:
            self.seats.pop(idx)
            self._renumber()
            self._refresh_seats()
            self.store.save_players(self.seats)

    def _move_seat(self, delta: int) -> None:
        idx = self._current_seat_index()
        new = idx + delta
        if idx < 0 or new < 0 or new >= len(self.seats):
            return
        self.seats[idx], self.seats[new] = self.seats[new], self.seats[idx]
        self._renumber()
        self._refresh_seats()
        self.seat_table.selectRow(new)
        self.store.save_players(self.seats)

    def _renumber(self) -> None:
        for i, s in enumerate(self.seats):
            s.pid = f"p{i + 1}"

    # ------------------------------------------------------------------
    # 对局控制
    # ------------------------------------------------------------------
    def _edit_game(self) -> None:
        dlg = GameDialog(self._game_spec.get("kind", "process"),
                         self._game_spec.get("target", ""),
                         self._game_spec.get("args", ""),
                         self._game_spec.get("config") or {},
                         recent=self.settings.get("recent_games") or [], parent=self)
        if dlg.exec():
            self._game_spec = dlg.result_spec()
            self._save_game_spec()
            try:
                self.store.remember_game(self._game_spec.get("kind", "process"),
                                         self._game_spec.get("target", ""),
                                         self._game_spec.get("args", ""))
                self.settings = self.store.load_settings()
            except Exception:
                pass
            self._refresh_game_bar()

    def _edit_settings(self) -> None:
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec():
            self.settings = dlg.result_settings()
            self.store.save_settings(self.settings)
            self.seed_spin.setValue(int(self.settings.get("seed", 1)))
            self.timeout_spin.setValue(int(self.settings.get("default_timeout_ms", 2000)))
            self.violation_spin.setValue(int(self.settings.get("max_violations", 3)))
            self.persistent_box.setChecked(bool(self.settings.get("persistent_players", True)))
            self.retry_box.setChecked(bool(self.settings.get("retry_invalid", False)))

    def _sync_settings_from_bar(self) -> None:
        self.settings["seed"] = self.seed_spin.value()
        self.settings["default_timeout_ms"] = self.timeout_spin.value()
        self.settings["max_violations"] = self.violation_spin.value()
        self.settings["persistent_players"] = self.persistent_box.isChecked()
        self.settings["retry_invalid"] = self.retry_box.isChecked()
        self.store.save_settings(self.settings)

    def _make_config(self) -> MatchConfig:
        return MatchConfig(
            seed=self.seed_spin.value(),
            max_violations=self.violation_spin.value(),
            persistent_players=self.persistent_box.isChecked(),
            retry_invalid=self.retry_box.isChecked(),
            default_timeout_ms=self.timeout_spin.value(),
        )

    def _game_factory(self, hooks: dict):
        kind = self._game_spec.get("kind", "process")
        target = self._game_spec.get("target", "")
        args = self._game_spec.get("args", "")
        cfg = self._game_spec.get("config") or {}
        seed = self.seed_spin.value()

        def on_game_log(text, level="info"):
            cb = hooks.get("on_log")
            if cb:
                cb({
                    "seq": 0,
                    "ts": datetime.now().strftime("%H:%M:%S.%f")[:-3],
                    "kind": "game", "turn": 0, "player": "", "text": text,
                })

        def factory():
            return make_game(kind, target, args=args, seed=seed, config=cfg,
                             on_log=on_game_log)

        return factory

    def _validate_start(self) -> bool:
        target = self._game_spec.get("target", "")
        if not target or not os.path.exists(target):
            QMessageBox.warning(self, "无法开始", "请先选择一个存在的游戏程序。")
            return False
        if not any(s.enabled for s in self.seats):
            QMessageBox.warning(self, "无法开始", "至少启用一个玩家席位。")
            return False
        return True

    def _start_match(self, step_mode: bool) -> None:
        if self.thread and self.thread.isRunning():
            return
        if not self._validate_start():
            return
        self._clear_log()
        self._reset_seat_status()
        hooks: dict = {}
        engine = MatchEngine(self._game_factory(hooks), self.seats, self._make_config(),
                             hooks, log_dir=self.store.log_dir)
        thread = MatchThread(engine, self)
        hooks["on_log"] = thread.sig_log.emit
        thread.sig_log.connect(self._append_log)
        thread.sig_finish.connect(self._on_match_finish)
        self.thread = thread
        if step_mode:
            engine.pause()
        thread.start()
        self._set_running(True)
        if step_mode:
            engine.step()
            self._paused = True          # 与引擎的 paused 保持一致
            self.btn_pause.setText("继续")
        self.status_label.setText("对局进行中…")

    def _toggle_pause(self) -> None:
        if not self.thread or not self.thread.isRunning():
            return
        engine = self.thread.engine
        if self._paused:
            engine.resume()
            self._paused = False
            self.btn_pause.setText("暂停")
            self.status_label.setText("对局进行中…")
        else:
            engine.pause()
            self._paused = True
            self.btn_pause.setText("继续")
            self.status_label.setText("已暂停，点「单步」走一回合")

    _paused = False

    def _step(self) -> None:
        if not self.thread or not self.thread.isRunning():
            self._start_match(True)
            return
        self.thread.engine.step()
        self._paused = True
        self.btn_pause.setText("继续")
        self.status_label.setText("单步中…")

    def _stop_match(self) -> None:
        if self.thread and self.thread.isRunning():
            self.thread.engine.stop()
            self.status_label.setText("正在停止…")

    def _set_running(self, running: bool) -> None:
        self.btn_start.setEnabled(not running)
        self.btn_pause.setEnabled(running)
        self.btn_step.setEnabled(True)
        self.btn_stop.setEnabled(running)
        self.btn_rule.setEnabled(not running)
        if not running:
            self._paused = False
            self.btn_pause.setText("暂停")

    # ------------------------------------------------------------------
    # 日志与结算
    # ------------------------------------------------------------------
    def _clear_log(self) -> None:
        self.log_table.setRowCount(0)
        self.detail_view.clear()
        self.result_table.setRowCount(0)
        self.result_tip.setText("对局进行中…")

    def _name_of(self, pid: str) -> str:
        for s in self.seats:
            if s.pid == pid:
                return s.name
        return pid

    def _append_log(self, entry: dict) -> None:
        t = self.log_table
        if t.rowCount() > 4000:
            for _ in range(500):
                t.removeRow(0)
        row = t.rowCount()
        t.insertRow(row)
        kind = entry.get("kind", "")
        text = entry.get("text", "")
        data = entry.get("data") or {}
        if kind == "action" and data.get("status") == "ok":
            act = json.dumps(data.get("action"), ensure_ascii=False)
            if len(act) > 90:
                act = act[:90] + "…"
            text = f"{text} {act}"
        elif kind == "violation" and data.get("kind"):
            text = f"[{data['kind']}] {text}"
        vals = [
            str(entry.get("seq") or ""),
            entry.get("ts", ""),
            str(entry.get("turn") or ""),
            self._name_of(entry.get("player", "")) if entry.get("player") else "",
            KIND_LABEL.get(kind, kind),
            text,
        ]
        color = QColor(KIND_COLOR.get(kind, "#374151"))
        for col, val in enumerate(vals):
            item = QTableWidgetItem(val)
            item.setForeground(QBrush(color))
            if col in (0, 2):
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if col == 0:
                item.setData(Qt.ItemDataRole.UserRole, entry)
            t.setItem(row, col, item)
        t.scrollToBottom()

        if kind == "turn":
            self.status_label.setText(f"进行中 · 第 {entry.get('turn')} 回合")
        elif kind == "violation" and entry.get("player"):
            self._set_seat_status(entry["player"], "违规")
        elif kind == "result":
            self.status_label.setText("对局结束")

    def _set_seat_status(self, pid: str, text: str) -> None:
        for row in range(self.seat_table.rowCount()):
            item = self.seat_table.item(row, 1)
            if item and item.data(Qt.ItemDataRole.UserRole) == pid:
                cell = self.seat_table.item(row, 4)
                if cell:
                    cell.setText(text)
                    cell.setForeground(QBrush(QColor("#dc2626" if text in ("违规", "判负") else "#374151")))
                break

    def _reset_seat_status(self) -> None:
        """开局前清掉上一局残留的「违规 / 判负」。

        以前只在编辑席位时才刷新这张表，于是新一局还没开始，状态栏就挂着上一局的红色。
        """
        for row in range(self.seat_table.rowCount()):
            cell = self.seat_table.item(row, 4)
            if cell:
                cell.setText("待命")
                cell.setForeground(QBrush(QColor("#374151")))

    def _on_log_selected(self) -> None:
        rows = self.log_table.selectionModel().selectedRows()
        if not rows:
            return
        item = self.log_table.item(rows[0].row(), 0)
        if not item:
            return
        entry = item.data(Qt.ItemDataRole.UserRole)
        if entry is None:
            return
        self.detail_view.setPlainText(json.dumps(entry, ensure_ascii=False, indent=2))

    def _on_match_finish(self, report) -> None:
        self._set_running(False)
        if report is None:
            err = self.thread.err if self.thread else ""
            self.status_label.setText("对局异常结束")
            self.result_tip.setText(f"异常：{err}")
            return
        self._fill_result(report)
        if report.error:
            self.status_label.setText(f"结束（{report.error}）")
        else:
            self.status_label.setText(f"对局结束 · {report.turns} 回合 · {report.duration_ms} ms")

    def _fill_result(self, report) -> None:
        rows = report.ranking_with_status()
        t = self.result_table
        t.setRowCount(0)
        for r in rows:
            row = t.rowCount()
            t.insertRow(row)
            vals = [str(r["rank"]), r["name"], f"{r['score']:g}", str(r["violations"]),
                    f"{r['avg_ms']:.1f}"]
            for col, val in enumerate(vals):
                item = QTableWidgetItem(val)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if r["disqualified"]:
                    item.setForeground(QBrush(QColor("#9ca3af")))
                elif r["rank"] == 1:
                    item.setForeground(QBrush(QColor("#b45309")))
                t.setItem(row, col, item)
            if r["disqualified"]:
                self._set_seat_status(r["pid"], "判负")

        bits = []
        if report.result and report.result.reason:
            bits.append(report.result.reason)
        if report.disqualified:
            bits.append("判负：" + "、".join(self._name_of(p) for p in report.disqualified))
        if report.aborted:
            bits.append("已手动中止")
        if report.error:
            bits.append(report.error)
        bits.append(f"日志：{os.path.basename(report.log_path)}" if report.log_path else "")
        self.result_tip.setText(" · ".join(b for b in bits if b))

    # ------------------------------------------------------------------
    # 批量
    # ------------------------------------------------------------------
    def _start_batch(self) -> None:
        if self.batch_thread and self.batch_thread.isRunning():
            return
        if not self._validate_start():
            return
        self.settings["batch_rounds"] = self.batch_rounds.value()
        self.store.save_settings(self.settings)

        hooks: dict = {}
        runner = BatchRunner(
            self._game_factory(hooks), self.seats, self._make_config(),
            BatchConfig(rounds=self.batch_rounds.value(), seed_base=self.batch_seed.value()),
            hooks, log_dir=self.store.log_dir, batch_dir=self.store.batch_dir,
        )
        thread = BatchThread(runner, self)
        hooks["on_progress"] = thread.sig_progress.emit
        # 在引擎线程里先过滤，只有关键事件才走信号队列，避免上千局的回合日志淹掉界面
        hooks["on_match_log"] = lambda entry: (
            thread.sig_log.emit(entry) if entry.get("kind") in BATCH_LOG_KINDS else None)
        thread.sig_progress.connect(self._on_batch_progress)
        thread.sig_log.connect(self._append_batch_log)
        thread.sig_finish.connect(self._on_batch_finish)
        self.batch_thread = thread
        self.batch_bar.setRange(0, self.batch_rounds.value())
        self.batch_bar.setValue(0)
        self.batch_table.setRowCount(0)
        self.batch_log.clear()
        self._reset_seat_status()
        self.batch_tip.setText("批量运行中…")
        self.btn_batch_start.setEnabled(False)
        self.btn_batch_stop.setEnabled(True)
        self.btn_batch_csv.setEnabled(False)
        thread.start()

    def _stop_batch(self) -> None:
        if self.batch_thread and self.batch_thread.isRunning():
            self.batch_thread.runner.stop()
            self.batch_tip.setText("正在停止…")

    def _on_batch_progress(self, done: int, total: int, report) -> None:
        self.batch_bar.setRange(0, total)
        self.batch_bar.setValue(done)
        if report is not None and getattr(report, "result", None):
            names = [self._name_of(p) for p in report.result.ranking[:3]]
            self.batch_tip.setText(f"第 {done}/{total} 局完成　" + " > ".join(names))

    def _append_batch_log(self, entry: dict) -> None:
        kind = entry.get("kind", "")
        if kind not in BATCH_LOG_KINDS:
            return
        text = str(entry.get("text", "")).strip()
        pid = entry.get("player") or ""
        if pid:
            text = f"{self._name_of(pid)}：{text}"
        turn = entry.get("turn") or 0
        prefix = f"第{turn}回合 " if turn else ""
        self.batch_log.appendPlainText(
            f"[{entry.get('ts', '')}] {prefix}{KIND_LABEL.get(kind, kind)} · {text}")

    def _on_batch_finish(self, report) -> None:
        self.btn_batch_start.setEnabled(True)
        self.btn_batch_stop.setEnabled(False)
        if report is None:
            self.batch_tip.setText("批量异常结束")
            return
        t = self.batch_table
        t.setRowCount(0)
        keys = ["rank", "name", "matches", "wins", "win_rate", "top2_rate",
                "avg_rank", "avg_score", "violations", "dq", "avg_ms"]
        for r in report.rows:
            row = t.rowCount()
            t.insertRow(row)
            for col, k in enumerate(keys):
                item = QTableWidgetItem(str(r.get(k, "")))
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if r.get("rank") == 1:
                    item.setForeground(QBrush(QColor("#b45309")))
                t.setItem(row, col, item)
        self._batch_csv_path = report.csv_path
        self.btn_batch_csv.setEnabled(bool(report.csv_path))
        msg = f"完成 {report.finished}/{report.rounds} 局 · 用时 {report.duration_ms / 1000:.1f}s"
        if report.errors:
            msg += f" · 异常 {len(report.errors)} 次"
        if report.csv_path:
            msg += f" · CSV：{os.path.basename(report.csv_path)}"
        self.batch_tip.setText(msg)

    def _export_batch_csv(self) -> None:
        if not self._batch_csv_path or not os.path.exists(self._batch_csv_path):
            return
        dst, _ = QFileDialog.getSaveFileName(self, "导出排行榜", "排行榜.csv", "CSV (*.csv)")
        if dst:
            try:
                with open(self._batch_csv_path, "r", encoding="utf-8-sig") as src, \
                        open(dst, "w", encoding="utf-8-sig") as out:
                    out.write(src.read())
                QMessageBox.information(self, "完成", f"已导出到：\n{dst}")
            except Exception as exc:
                QMessageBox.warning(self, "失败", str(exc))

    # ------------------------------------------------------------------
    # 杂项
    # ------------------------------------------------------------------
    def _open_path(self, path: str) -> None:
        if not path or not os.path.exists(path):
            QMessageBox.information(self, "提示", f"路径不存在：{path}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.abspath(path)))

    def _about(self) -> None:
        QMessageBox.about(
            self, "关于 conterX",
            "<b>conterX · 算法对抗中转站</b><br><br>"
            "上游游戏与下游算法程序之间的协议中继：<br>"
            "· 统一协议 conterx/1（stdin/stdout NDJSON）<br>"
            "· 私有视野裁剪、超时与违规判定<br>"
            "· 回合日志、批量对抗与排行榜<br><br>"
            "示例游戏：暗标竞拍（进程式）、抢21（插件式）")

    def _wait_thread(self, thread, stopper, label: str) -> bool:
        """先礼后兵：发停止信号 → 等 6s → 还没停就 terminate。

        QThread 在运行状态下被销毁会让 Qt 直接 abort（退出码 127），
        所以宁可强杀，也不能放着不管。
        """
        if thread is None or not thread.isRunning():
            return True
        try:
            stopper()
        except Exception:
            pass
        if thread.wait(6000):
            return True
        thread.terminate()
        thread.wait(1500)
        self.status_label.setText(f"{label}未能正常停止，已强制结束")
        return thread.isFinished()

    def closeEvent(self, event) -> None:
        ok1 = self._wait_thread(
            self.thread,
            lambda: self.thread.engine.stop() if self.thread else None,
            "对局")
        ok2 = self._wait_thread(
            self.batch_thread,
            lambda: self.batch_thread.runner.stop() if self.batch_thread else None,
            "批量任务")
        if not (ok1 and ok2):
            # 线程真的停不下来：不要关窗，让用户知道发生了什么
            QMessageBox.warning(self, "无法退出",
                                "后台线程仍未结束，为避免数据损坏暂不关闭窗口。\n"
                                "请稍后重试，或直接结束进程。")
            event.ignore()
            return
        try:
            self.store.save_players(self.seats)
            self._save_game_spec()
        except Exception:
            pass
        super().closeEvent(event)
