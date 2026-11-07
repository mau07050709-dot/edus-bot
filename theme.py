"""Paleta oscura compartida por ventanas, diálogos y controles."""

from PySide6.QtGui import QColor, QPalette


def apply_theme(app):
    app.setStyle("Fusion")
    palette = QPalette()
    colors = {
        "Window": "#212121", "WindowText": "#f5f5f5", "Base": "#262626",
        "AlternateBase": "#303030", "Text": "#f5f5f5", "PlaceholderText": "#bdbdbd",
        "Button": "#383838", "ButtonText": "#f5f5f5", "Highlight": "#315c89",
        "HighlightedText": "#ffffff", "ToolTipBase": "#303030", "ToolTipText": "#f5f5f5",
        "Link": "#91c5ff",
    }
    for role, color in colors.items():
        palette.setColor(getattr(QPalette.ColorRole, role), QColor(color))
    for role in ("WindowText", "Text", "ButtonText"):
        palette.setColor(QPalette.ColorGroup.Disabled, getattr(QPalette.ColorRole, role), QColor("#a6a6a6"))
    app.setPalette(palette)
    app.setStyleSheet("""
        QLabel#title { font-size: 27px; font-weight: 700; }
        QLabel#subtitle { color: #bdbdbd; }
        QLabel#connection { color: #a8d0fa; font-weight: 600; }
        QGroupBox { background: #2f2f2f; border: 1px solid #484848;
                    border-radius: 8px; margin-top: 10px; padding: 10px; }
        QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
        QPushButton, QLineEdit, QComboBox, QTimeEdit, QPlainTextEdit {
            background: #383838; color: #f5f5f5; border: 1px solid #595959;
            border-radius: 5px; padding: 5px 8px;
        }
        QPushButton:hover { background: #474747; }
        QPushButton#primary:enabled { background: #245785; border-color: #5281ad; font-weight: 600; }
        QPushButton#primary:hover { background: #306793; }
        QPushButton:disabled, QComboBox:disabled { background: #2c2c2c; color: #a6a6a6; }
        QPushButton:focus, QLineEdit:focus, QComboBox:focus, QTimeEdit:focus { border-color: #91c5ff; }
        QPlainTextEdit { background: #262626; }
        QToolTip { background: #303030; color: #f5f5f5; border: 1px solid #595959; }
    """)
