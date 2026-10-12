APP_STYLE = r"""
* {
    font-family: "Microsoft YaHei UI", "Segoe UI";
    font-size: 13px;
    color: #e6edf7;
}
QMainWindow, QWidget#appRoot, QDialog { background: #09101a; }
QWidget#titleBar { background: #0d1622; border-bottom: 1px solid #26364b; }
QLabel#brandTitle { color: #f7f9fc; font-size: 15px; font-weight: 700; }
QLabel#brandSubtitle, QLabel.muted { color: #8799af; }
QFrame#sideBar { background: #0d1622; border-right: 1px solid #223147; }
QPushButton.navButton {
    text-align: left; padding: 10px 14px; border: 0; border-radius: 8px;
    color: #9fb0c5; background: transparent;
}
QPushButton.navButton:hover { background: #16243a; color: #eef5ff; }
QPushButton.navButton:checked { background: #17365d; color: #8fc1ff; font-weight: 700; }
QFrame.card, QFrame.panel {
    background: #101a28; border: 1px solid #25364d; border-radius: 12px;
}
QFrame#progressCard { background: #102238; border: 1px solid #315e91; border-radius: 10px; }
QFrame#progressCard[state="failed"] { background: #2b1920; border-color: #824052; }
QFrame#progressCard[state="success"] { background: #10251f; border-color: #34725d; }
QFrame#recoveryCard { background: #2a1920; border: 1px solid #804357; border-radius: 10px; }
QLabel.pageTitle { color: #f7f9fc; font-size: 22px; font-weight: 700; }
QLabel.sectionTitle { color: #f0f4fa; font-size: 14px; font-weight: 700; }
QLabel#progressTitle { font-weight: 700; color: #eaf3ff; }
QLabel#progressDetail { color: #a8bed7; }
QLabel#elapsedLabel { color: #77aaff; }
QTextEdit, QPlainTextEdit, QLineEdit, QComboBox, QSpinBox {
    background: #08111d; border: 1px solid #2a3d57; border-radius: 8px;
    padding: 8px; selection-background-color: #2f72d6;
}
QTextEdit:focus, QPlainTextEdit:focus, QLineEdit:focus, QComboBox:focus, QSpinBox:focus {
    border-color: #518ee6;
}
QPushButton, QToolButton {
    background: #182942; border: 1px solid #314b6b; border-radius: 8px;
    padding: 8px 12px; color: #dce8f7;
}
QPushButton:hover, QToolButton:hover { background: #213958; border-color: #4c6d94; }
QPushButton:disabled { background: #111b29; color: #58687b; border-color: #223044; }
QPushButton#primaryButton { background: #2f72dc; border-color: #5796ef; color: white; font-weight: 700; }
QPushButton#primaryButton:hover { background: #3d80e8; }
QPushButton#dangerButton { background: #3b2028; border-color: #744252; color: #ffcbd4; }
QToolButton.windowButton { background: transparent; border: none; padding: 0; }
QToolButton.windowButton:hover { background: #24364d; }
QToolButton#closeButton:hover { background: #b63a4c; }
QTableWidget {
    background: #0b1421; alternate-background-color: #0e1a2a; border: 1px solid #23344b;
    border-radius: 8px; gridline-color: #1d2b3e;
}
QTableWidget::item { padding: 7px; }
QTableWidget::item:selected { background: #245d9f; }
QHeaderView::section {
    background: #162338; color: #a8b8cb; border: 0; border-bottom: 1px solid #2a3b51;
    padding: 8px; font-weight: 600;
}
QProgressBar { background: #07101b; border: 1px solid #2a3e59; border-radius: 5px; text-align: center; }
QProgressBar::chunk { background: #4487ec; border-radius: 4px; }
QMenuBar { background: transparent; }
QMenuBar::item { padding: 6px 9px; border-radius: 6px; color: #aebccf; }
QMenuBar::item:selected { background: #1b2b42; }
QMenu { background: #111c2b; border: 1px solid #2d4059; padding: 5px; }
QMenu::item { padding: 7px 28px 7px 10px; border-radius: 5px; }
QMenu::item:selected { background: #244873; }
QSplitter::handle { background: transparent; width: 6px; }
QScrollBar:vertical { background: transparent; width: 10px; }
QScrollBar::handle:vertical { background: #334960; border-radius: 5px; min-height: 24px; }
"""
