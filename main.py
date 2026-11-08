#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""conterX · 算法对抗中转站

上游游戏 ←→ 本软件 ←→ 下游算法程序（玩家 1/2/3…）

    python main.py

依赖：Python 3.10+ 与 PySide6（pip install pyside6）
"""

import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

try:
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication
except ImportError:
    sys.stderr.write(
        "缺少 PySide6。请先安装：\n"
        "    pip install pyside6\n"
        "或用带 PySide6 的解释器运行本文件。\n"
    )
    sys.exit(1)

from core.store import Store  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402

STYLE = """
QWidget { color: #1f2937; }
QMainWindow, QTabWidget::pane { background: #f4f5f7; }
QGroupBox {
    background: #ffffff;
    border: 1px solid #e3e6ea;
    border-radius: 8px;
    margin-top: 12px;
    padding: 10px 8px 8px 8px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    top: 2px;
    padding: 0 4px;
    color: #4b5563;
    font-weight: 600;
}
QTabWidget::pane { border: 1px solid #e3e6ea; border-radius: 8px; top: -1px; }
QTabBar::tab {
    background: transparent;
    padding: 8px 18px;
    margin-right: 2px;
    color: #6b7280;
    border-radius: 6px 6px 0 0;
}
QTabBar::tab:selected { background: #ffffff; color: #1f2937; font-weight: 600; }
QTabBar::tab:hover { color: #1f2937; }
QPushButton {
    background: #ffffff;
    border: 1px solid #d5d9df;
    border-radius: 6px;
    padding: 6px 14px;
    min-height: 18px;
}
QPushButton:hover { border-color: #9aa4b2; background: #fafbfc; }
QPushButton:pressed { background: #eef1f5; }
QPushButton:disabled { color: #b0b6bf; border-color: #e8eaee; background: #f7f8fa; }
QPushButton#primary {
    background: #3b6fd4;
    border: 1px solid #3b6fd4;
    color: #ffffff;
    font-weight: 600;
}
QPushButton#primary:hover { background: #3463c0; }
QPushButton#primary:disabled { background: #b9c9e8; border-color: #b9c9e8; color: #f2f5fb; }
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit {
    background: #ffffff;
    border: 1px solid #d5d9df;
    border-radius: 6px;
    padding: 4px 6px;
    selection-background-color: #cfe0fb;
}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border-color: #3b6fd4; }
QTableWidget {
    background: #ffffff;
    border: 1px solid #e3e6ea;
    border-radius: 6px;
    gridline-color: #f0f2f5;
    selection-background-color: #e8f0fd;
    selection-color: #1f2937;
}
QHeaderView::section {
    background: #f7f8fa;
    border: none;
    border-bottom: 1px solid #e3e6ea;
    padding: 6px 4px;
    color: #6b7280;
    font-weight: 600;
}
QProgressBar {
    background: #eef1f5;
    border: none;
    border-radius: 5px;
    height: 12px;
    text-align: center;
    color: #4b5563;
}
QProgressBar::chunk { background: #3b6fd4; border-radius: 5px; }
QSplitter::handle { background: transparent; }
QStatusBar { background: #ffffff; border-top: 1px solid #e3e6ea; color: #6b7280; }
QMenuBar { background: #ffffff; }
QMenuBar::item:selected { background: #eef1f5; }
"""


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("conterX")
    app.setApplicationDisplayName("conterX · 算法对抗中转站")
    font = QFont("Microsoft YaHei UI", 10)
    app.setFont(font)
    app.setStyleSheet(STYLE)

    store = Store(BASE)
    win = MainWindow(store)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
